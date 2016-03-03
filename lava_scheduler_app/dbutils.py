"""
Database utility functions which use but are not actually models themselves
Used to allow models.py to be shortened and easier to follow.
"""

# pylint: disable=wrong-import-order

import os
import yaml
import jinja2
import datetime
import logging
import simplejson
import django
from django.db.models import Q
from django.db import IntegrityError, transaction
from django.contrib.auth.models import User
from django.utils import timezone
from linaro_django_xmlrpc.models import AuthToken
from lava_scheduler_app.models import (
    DeviceDictionary,
    Device,
    DeviceType,
    TestJob,
    TemporaryDevice,
    JSONDataError,
    validate_job,
    is_deprecated_json,
)
from lava_results_app.dbutils import map_metadata
from lava_dispatcher.pipeline.device import PipelineDevice
from lava_dispatcher.pipeline.parser import JobParser
from lava_dispatcher.pipeline.action import JobError

# pylint: disable=too-many-branches,too-many-statements,too-many-locals


def match_vlan_interface(device, job_def):
    if not isinstance(job_def, dict):
        raise RuntimeError("Invalid vlan interface data")
    if 'protocols' not in job_def or 'lava-vland' not in job_def['protocols']:
        return False
    interfaces = []
    logger = logging.getLogger('dispatcher-master')
    for vlan_name in job_def['protocols']['lava-vland']:
        tag_list = job_def['protocols']['lava-vland'][vlan_name]['tags']
        device_dict = DeviceDictionary.get(device.hostname).to_dict()
        if 'tags' not in device_dict['parameters']:
            return False
        for interface, tags in device_dict['parameters']['tags'].iteritems():
            if any(set(tags).intersection(tag_list)) and interface not in interfaces:
                logger.debug("Matched vlan %s to interface %s on %s", vlan_name, interface, device)
                interfaces.append(interface)
                # matched, do not check any further interfaces of this device for this vlan
                break
    return len(interfaces) == len(job_def['protocols']['lava-vland'].keys())


def initiate_health_check_job(device):
    if device.status in [Device.RETIRED]:
        return None

    existing_health_check_job = device.get_existing_health_check_job()
    if existing_health_check_job:
        return existing_health_check_job

    job_data = device.device_type.health_check_job
    user = User.objects.get(username='lava-health')
    if not job_data:
        # This should never happen, it's a logic error.
        device.put_into_maintenance_mode(
            user, "health check job not found in initiate_health_check_job")
        raise JSONDataError("no health check job found for %r", device.hostname)
    return testjob_submission(job_data, user, check_device=device)


def submit_health_check_jobs():
    """
    Checks which devices need a health check job and submits the needed
    health checks.
    Looping is only active once a device is offline.
    """

    logger = logging.getLogger('dispatcher-master')
    for device in Device.objects.filter(
            Q(status=Device.IDLE) | Q(status=Device.OFFLINE, health_status=Device.HEALTH_LOOPING)):
        time_denominator = True
        if device.device_type.health_denominator == DeviceType.HEALTH_PER_JOB:
            time_denominator = False
        if not device.device_type.health_check_job:
            run_health_check = False
        elif device.health_status == Device.HEALTH_UNKNOWN:
            run_health_check = True
        elif device.health_status == Device.HEALTH_LOOPING:
            run_health_check = True
        elif not device.last_health_report_job:
            run_health_check = True
        elif not device.last_health_report_job.end_time:
            run_health_check = True
        else:
            if time_denominator:
                run_health_check = device.last_health_report_job.end_time < \
                    timezone.now() - datetime.timedelta(hours=device.device_type.health_frequency)
                if run_health_check:
                    logger.debug("%s needs to run_health_check", device)
                    logger.debug("health_check_end=%s", device.last_health_report_job.end_time)
                    logger.debug("health_frequency is every %s hours", device.device_type.health_frequency)
                    logger.debug("time_diff=%s", (
                        timezone.now() - datetime.timedelta(hours=device.device_type.health_frequency)))
            else:
                unchecked_job_count = TestJob.objects.filter(
                    actual_device=device, health_check=False,
                    id__gte=device.last_health_report_job.id).count()
                run_health_check = unchecked_job_count > device.device_type.health_frequency
                if run_health_check:
                    logger.debug("%s needs to run_health_check", device)
                    logger.debug("unchecked_job_count=%s", unchecked_job_count)
                    logger.debug("health_frequency is every %s jobs", device.device_type.health_frequency)

        if run_health_check:
            logger.debug('submit health check for %s', device.hostname)
            try:
                initiate_health_check_job(device)
            except (yaml.YAMLError, JSONDataError):
                # already logged, don't allow the daemon to fail.
                pass


def testjob_submission(job_definition, user, check_device=None):
    """
    Single submission frontend for JSON or YAML
    :param job_definition: string of the job submission
    :param user: user attempting the submission
    :param: check_device: set specified device as the target
    and set job as a health check job. (JSON only)
    :return: a job or a list of jobs
    :raises: SubmissionException, Device.DoesNotExist,
        DeviceType.DoesNotExist, DevicesUnavailableException,
        JSONDataError, JSONDecodeError, ValueError
    """

    if is_deprecated_json(job_definition):
        allow_health = False
        if check_device:
            job_json = simplejson.loads(job_definition)
            job_json['target'] = check_device.hostname
            job_json['health-check'] = True
            job_definition = simplejson.dumps(job_json)
            allow_health = True
        try:
            job = TestJob.from_json_and_user(job_definition, user, health_check=allow_health)
            job.health_check = True
            job.requested_device = check_device
            job.save(update_fields=['health_check', 'requested_device'])
        except (JSONDataError, ValueError) as exc:
            check_device.put_into_maintenance_mode(
                user, "Job submission failed for health job for %s: %s" % (check_device, exc))
            raise JSONDataError("Health check job submission failed for %s: %s" % (check_device, exc))
    else:
        validate_job(job_definition)
        job = TestJob.from_yaml_and_user(job_definition, user)
        if check_device and isinstance(check_device, Device) and not isinstance(job, list):
            # the slave must neither know nor care if this is a health check,
            # only the master cares and that has the database connection.
            job.health_check = True
            job.requested_device = check_device
            job.save(update_fields=['health_check', 'requested_device'])
    return job


def find_device_for_job(job, device_list):  # pylint: disable=too-many-branches
    """
    If the device has the same tags as the job or all the tags required
    for the job and some others which the job does not explicitly specify,
    check if this device be assigned to this job for this user.
    Works for pipeline jobs and old-style jobs but refuses to select a
    non-pipeline device for a pipeline job. Pipeline devices are explicitly
    allowed to run non-pipeline jobs.

    Note: with a large queue and a lot of devices, this function can be a
    significant delay.
    """
    if job.dynamic_connection:
        # secondary connection, the "host" has a real device
        return None

    logger = logging.getLogger('dispatcher-master')
    for device in device_list:
        if device.current_job:
            if device.device_type != job.requested_device_type:
                continue
            if job.requested_device and device == job.requested_device:
                # forced health checks complicate this condition as it would otherwise
                # be an error to find the device here when it should not be IDLE.
                continue
            # warn the admin that this needs human intervention
            bad_job = TestJob.objects.get(id=device.current_job.id)
            logger.warning("Refusing to reserve %s for %s - current job is %s",
                           device, job, bad_job)
            device_list.remove(device)
        if device.is_exclusive and not job.is_pipeline:
            continue
    if not device_list:
        return None
    # forced health check support
    if job.health_check is True:
        if job.requested_device and job.requested_device.status == Device.OFFLINE:
            logger.debug("[%s] - assigning %s for forced health check.", job.id, job.requested_device)
            return job.requested_device
    logger.debug("[%s] Finding a device from a list of %s", job.id, len(device_list))
    for device in device_list:
        if job.is_vmgroup:
            # special handling, tied directly to the TestJob within the vmgroup
            # mask to a Temporary Device to be able to see vm_group of the device
            tmp_dev = TemporaryDevice.objects.filter(hostname=device.hostname)
            if tmp_dev and job.vm_group != tmp_dev[0].vm_group:
                continue
        if job.is_pipeline and not device.is_pipeline:
            continue
        if device == job.requested_device:  # for pipeline, this is only used for automated health checks
            if device.can_submit(job.submitter) and\
                    set(job.tags.all()) & set(device.tags.all()) == set(job.tags.all()):
                return device
        if device.device_type == job.requested_device_type:
            if device.can_submit(job.submitter) and\
                    set(job.tags.all()) & set(device.tags.all()) == set(job.tags.all()):
                return device
    return None


def get_available_devices():
    """
    A list of idle devices, with private devices first.

    This order is used so that a job submitted by John Doe will prefer
    using John Doe's private devices over using public devices that could
    be available for other users who don't have their own.

    Forced health checks ignore this constraint.
    """
    devices = Device.objects.filter(status=Device.IDLE).order_by('is_public')
    return devices


def get_job_queue():
    """
    Order of precedence:

    - health checks before everything else
    - all the rest of the jobs, sorted by priority, then submission time.

    Additionally, we also sort by target_group, so that if you have two
    multinode job groups with the same priority submitted at the same time,
    their sub jobs will be contiguous to each other in the list.  Lastly,
    we also sort by id to make sure we have a stable order and that jobs
    that came later into the system (as far as the DB is concerned) get
    later into the queue.

    Pipeline jobs are allowed to be assigned but the actual running of
    a job on a reserved pipeline device is down to the dispatcher-master.
    """

    logger = logging.getLogger('dispatcher-master')
    jobs = TestJob.objects.filter(status=TestJob.SUBMITTED)
    jobs = jobs.filter(actual_device=None)
    jobs = jobs.order_by('-health_check', '-priority', 'submit_time',
                         'vm_group', 'target_group', 'id')

    if len(jobs):
        logger.info("Job queue length: %d", len(jobs))
    return jobs


def _validate_queue():
    """
    Invalid reservation states can leave zombies which are SUBMITTED with an actual device.
    These jobs get ignored by the get_job_queue function and therfore by assign_jobs *unless*
    another job happens to reference that specific device.
    """
    logger = logging.getLogger('dispatcher-master')
    jobs = TestJob.objects.filter(status=TestJob.SUBMITTED)
    jobs = jobs.filter(actual_device__isnull=False)
    for job in jobs:
        if not job.actual_device.current_job:
            device = Device.objects.get(hostname=job.actual_device.hostname)
            if device.status != Device.IDLE:
                continue
            logger.warning(
                "Fixing up a broken device reservation for queued %s on %s", job, device.hostname)
            device.status = Device.RESERVED
            device.current_job = job
            device.save(update_fields=['status', 'current_job'])


def _validate_idle_device(job, device):
    """
    The problem here is that instances with a lot of devices would spend a lot of time
    refetching all of the device details every scheduler tick when it is only under
    particular circumstances that an error is made. The safe option is always to refuse
    to use a device which has changed status.
    get() evaluates immediately.
    :param job: job to have a device assigned
    :param device: device to refresh and check
    :return: True if device can be reserved
    """
    # FIXME: do this properly in the dispatcher master.
    # FIXME: isolate forced health check requirements
    if django.VERSION >= (1, 8):
        # https://docs.djangoproject.com/en/dev/ref/models/instances/#refreshing-objects-from-database
        device.refresh_from_db()
    else:
        device = Device.objects.get(hostname=device.hostname)

    logger = logging.getLogger('dispatcher-master')
    # to be valid for reservation, no queued TestJob can reference this device
    jobs = TestJob.objects.filter(
        status__in=[TestJob.RUNNING, TestJob.SUBMITTED, TestJob.CANCELING],
        actual_device=device)
    if jobs:
        logger.warning(
            "%s (which has current_job %s) is already referenced by %d jobs %s",
            device.hostname, device.current_job, len(jobs), [job.id for job in jobs])
        if len(jobs) == 1:
            logger.warning(
                "Fixing up a broken device reservation for %s on %s",
                jobs[0], device.hostname)
            device.status = Device.RESERVED
            device.current_job = jobs[0]
            device.save(update_fields=['status', 'current_job'])
            return False
    # forced health check support
    if job.health_check:
        # only assign once the device is offline.
        if device.status not in [Device.OFFLINE, Device.IDLE]:
            logger.warning("Refusing to reserve %s for health check, not IDLE or OFFLINE", device)
            return False
    elif device.status is not Device.IDLE:
        logger.warning("Refusing to reserve %s which is not IDLE", device)
        return False
    if device.current_job:
        logger.warning("Device %s already has a current job", device)
        return False
    return True


def _validate_non_idle_devices(reserved_devices, idle_devices):
    """
    only check those devices which we *know* should have been changed
    and check that the changes are correct.
    """
    errors = []
    logger = logging.getLogger('dispatcher-master')
    for device_name in reserved_devices:
        device = Device.objects.get(hostname=device_name)  # force re-load
        if device.status not in [Device.RESERVED, Device.RUNNING]:
            logger.warning("Failed to properly reserve %s", device)
            errors.append('r')
        if device in idle_devices:
            logger.warning("%s is still listed as available!", device)
            errors.append('a')
        if not device.current_job:
            logger.warning("Invalid reservation, %s has no current job.", device)
            return False
        if not device.current_job.actual_device:
            logger.warning("Invalid reservation, %s has no actual device.", device.current_job)
            return False
        if device.hostname != device.current_job.actual_device.hostname:
            logger.warning(
                "%s is not the same device as %s", device, device.current_job.actual_device)
            errors.append('j')
    return errors == []


def assign_jobs():
    """
    Check all jobs against all available devices and assign only if all conditions are met
    This routine needs to remain fast, so has to manage local cache variables of device status but
    still cope with a job queue over 1,000 and a device matrix of over 100. The main load is in
    find_device_for_job as *all* jobs in the queue must be checked at each tick. (A job far back in
    the queue may be the only job which exactly matches the most recent devices to become available.)

    When viewing the logs of these operations, the device will be Idle when Assigning to a Submitted
    job. That job may be for a device_type or a specific device (typically health checks use a specific
    device). The device will be Reserved when Assigned to a Submitted job on that device - the type will
    not be mentioned. The total number of assigned jobs and devices will be output at the end of each tick.
    Finally, the reserved device is removed from the local cache of available devices.

    Warnings are emitted if the device states are not as expected, before or after assignment.
    """
    # FIXME: once scheduler daemon is disabled, implement as in share/zmq/assign.[dia|png]
    # FIXME: Make the forced health check constraint explicit
    # evaluate the testjob query set using list()

    logger = logging.getLogger('dispatcher-master')
    _validate_queue()
    jobs = list(get_job_queue())
    if not jobs:
        return
    assigned_jobs = []
    reserved_devices = []
    # this takes a significant amount of time when under load, only do it once per tick
    devices = list(get_available_devices())
    # a forced health check can be assigned even if the device is not in the list of idle devices.
    for job in jobs:
        device = find_device_for_job(job, devices)
        if device:
            if job.is_pipeline:
                job_dict = yaml.load(job.definition)
                if 'protocols' in job_dict and 'lava-vland' in job_dict['protocols']:
                    if not match_vlan_interface(device, job_dict):
                        logger.debug("%s does not match vland tags", str(device.hostname))
                        devices.remove(device)
                        continue
            if not _validate_idle_device(job, device):
                logger.debug("Removing %s from the list of available devices",
                             str(device.hostname))
                devices.remove(device)
                continue
            logger.info("Assigning %s for %s", device, job)
            # avoid catching exceptions inside atomic (exceptions are slow too)
            # https://docs.djangoproject.com/en/1.7/topics/db/transactions/#controlling-transactions-explicitly
            if AuthToken.objects.filter(user=job.submitter).count():
                job.submit_token = AuthToken.objects.filter(user=job.submitter).first()
            else:
                job.submit_token = AuthToken.objects.create(user=job.submitter)
            try:
                # Make this sequence atomic
                with transaction.atomic():
                    job.actual_device = device
                    job.save()
                    device.current_job = job
                    # implicit device save in state_transition_to()
                    device.state_transition_to(
                        Device.RESERVED, message="Reserved for job %s" % job.display_id, job=job)
            except IntegrityError:
                # Retry in the next call to _assign_jobs
                logger.warning(
                    "Transaction failed for job %s, device %s", job.display_id, device.hostname)
            assigned_jobs.append(job.id)
            reserved_devices.append(device.hostname)
            logger.info("Assigned %s to %s", device, job)
            if device in devices:
                logger.debug("Removing %s from the list of available devices", str(device.hostname))
                devices.remove(device)
    # re-evaluate the devices query set using list() now that the job loop is complete
    devices = list(get_available_devices())
    postprocess = _validate_non_idle_devices(reserved_devices, devices)
    if postprocess and reserved_devices:
        logger.debug("All queued jobs checked, %d devices reserved and validated", len(reserved_devices))

    # worker heartbeat must not occur within this loop
    logger.info("Assigned %d jobs on %s devices", len(assigned_jobs), len(reserved_devices))


def create_job(job, device):
    """
    Only for use with the dispatcher-master
    """
    # FIXME check the incoming device status
    job.actual_device = device
    device.current_job = job
    new_status = Device.RESERVED
    msg = "Reserved for job %d" % job.id
    device.state_transition_to(new_status, message=msg, job=job)
    device.status = new_status
    # Save the result
    job.save()
    device.save()


def start_job(job):
    """
    Only for use with the dispatcher-master
    """
    job.status = TestJob.RUNNING
    # TODO: Only if that was not already the case !
    job.start_time = timezone.now()
    device = job.actual_device
    msg = "Job %d running" % job.id
    new_status = Device.RUNNING
    job.save()
    if not job.dynamic_connection:
        device.state_transition_to(new_status, message=msg, job=job)
        device.status = new_status
        # Save the result
        device.save()


def fail_job(job, fail_msg=None, job_status=TestJob.INCOMPLETE):
    """
    Fail the job due to issues which would compromise any other jobs
    in the same multinode group.
    If not multinode, simply wraps end_job.
    """
    if not job.is_multinode:
        end_job(job, fail_msg=fail_msg, job_status=job_status)
        return
    for failed_job in job.sub_jobs_list:
        end_job(failed_job, fail_msg=fail_msg, job_status=job_status)


def handle_health(job, new_device_status):
    """
    LOOPING = no change
    job is not health check = no change
    last_health_report_job is set
    INCOMPLETE = HEALTH_FAIL, maintenance_mode
    COMPLETE = HEALTH_PASS, device IDLE
    Only change the device here, job is not returned and
    should not be saved.
    """
    device = job.actual_device
    device.status = new_device_status
    if not job.health_check or device.health_status == Device.HEALTH_LOOPING:
        return device
    device.last_health_report_job = job
    if job.status == TestJob.INCOMPLETE:
        device.health_status = Device.HEALTH_FAIL
        user = User.objects.get(username='lava-health')
        device.put_into_maintenance_mode(user, "Health Check Job Failed")
    elif job.status == TestJob.COMPLETE:
        device.health_status = Device.HEALTH_PASS
    elif job.status == TestJob.CANCELED:
        device.health_status = Device.HEALTH_UNKNOWN
    return device


def end_job(job, fail_msg=None, job_status=TestJob.COMPLETE):
    """
    Controls the end of a single job..
    If the job failed rather than simply ended with an exit code, use fail_job.
    """
    if job.status in [TestJob.COMPLETE, TestJob.INCOMPLETE, TestJob.CANCELED]:
        # testjob has already ended and been marked as such
        return
    job.status = job_status
    if job.status == TestJob.CANCELING:
        job.status = TestJob.CANCELED
    if job.start_time and not job.end_time:
        job.end_time = timezone.now()
    device = job.actual_device
    if fail_msg:
        job.failure_comment = "%s %s" % (job.failure_comment, fail_msg) if job.failure_comment else fail_msg
    if not device:
        job.save()
        return
    msg = "Job %d has ended. Setting job status %s" % (job.id, TestJob.STATUS_CHOICES[job.status][1])
    device = handle_health(job, Device.IDLE)
    device.state_transition_to(device.status, message=msg, job=job)
    device.current_job = None
    # Save the result
    job.save()
    device.save()


def cancel_job(job):
    job.status = TestJob.CANCELED
    job.end_time = timezone.now()
    if job.dynamic_connection:
        job.save()
        return
    msg = "Job %d cancelled" % job.id
    device = handle_health(job, Device.IDLE)
    device.state_transition_to(device.status, message=msg, job=job)
    if device.current_job and device.current_job == job:
        device.current_job = None
    # Save the result
    job.save()
    device.save()


def select_device(job, dispatchers):  # pylint: disable=too-many-return-statements
    """
    Transitioning a device from Idle to Reserved is the responsibility of the scheduler_daemon (currently).
    This function just checks that the reserved device is valid for this job.
    Jobs will only enter this function if a device is already reserved for that job.
    Stores the pipeline description

    To prevent cycling between lava_scheduler_daemon:assign_jobs and here, if a job
    fails validation, the job is incomplete. Issues with this need to be fixed using
    device tags.
    """
    # FIXME: split out dynamic_connection, multinode and validation
    logger = logging.getLogger('dispatcher-master')
    if not job.dynamic_connection:
        if not job.actual_device:
            return None
        if job.actual_device.status is not Device.RESERVED:
            # should not happen
            logger.error("[%d] device [%s] not in reserved state", job.id, job.actual_device)
            return None

        if job.actual_device.worker_host is None:
            fail_msg = "Misconfigured device configuration for %s - missing worker_host" % job.actual_device
            fail_job(job, fail_msg=fail_msg)
            logger.error(fail_msg)
            return None

    if job.is_multinode:
        # inject the actual group hostnames into the roles for the dispatcher to populate in the overlay.
        devices = {}
        for multinode_job in job.sub_jobs_list:
            # build a list of all devices in this group
            definition = yaml.load(multinode_job.definition)
            # devices are not necessarily assigned to all jobs in a group at the same time
            # check all jobs in this multinode group before allowing any to start.
            if multinode_job.dynamic_connection:
                logger.debug("[%s] dynamic connection job", multinode_job.sub_id)
                continue
            if not multinode_job.actual_device:
                logger.debug("[%s] job has no device yet", multinode_job.sub_id)
                return None
            devices[str(multinode_job.actual_device.hostname)] = definition['protocols']['lava-multinode']['role']
        for multinode_job in job.sub_jobs_list:
            # apply the complete list to all jobs in this group
            definition = yaml.load(multinode_job.definition)
            definition['protocols']['lava-multinode']['roles'] = devices
            multinode_job.definition = yaml.dump(definition)
            multinode_job.save()

    # Load job definition to get the variables for template rendering
    job_def = yaml.load(job.definition)
    job_ctx = job_def.get('context', {})
    parser = JobParser()
    device = None
    device_object = None
    if not job.dynamic_connection:
        device = job.actual_device

        try:
            device_config = device.load_device_configuration(job_ctx)  # raw dict
        except (jinja2.TemplateError, yaml.YAMLError, IOError) as exc:
            logger.error("[%d] jinja2 error: %s", job.id, exc)
            msg = "Administrative error. Unable to parse device configuration: '%s'" % exc
            fail_job(job, fail_msg=msg)
            return None
        if not device_config or not isinstance(device_config, dict):
            # it is an error to have a pipeline device without a device dictionary as it will never get any jobs.
            msg = "Administrative error. Device '%s' has no device dictionary." % device.hostname
            logger.error('[%d] device-dictionary error: %s', job.id, msg)
            # as we don't control the scheduler, yet, this has to be an error and an incomplete job.
            # the scheduler_daemon sorts by a fixed order, so this would otherwise just keep on repeating.
            fail_job(job, fail_msg=msg)
            return None
        if not device.worker_host or not device.worker_host.hostname:
            msg = "Administrative error. Device '%s' has no worker host." % device.hostname
            logger.error('[%d] worker host error: %s', job.id, msg)
            fail_job(job, fail_msg=msg)
            return None
        if device.worker_host.hostname not in dispatchers:
            # a configured worker has not called in to this master
            # likely that the worker is misconfigured - polling the wrong master
            # or simply not running at all.
            msg = """Administrative error. Device '{0}' has a worker_host setting of
 '{1}' but no slave has registered with this master
 using that FQDN.""".format(device.hostname, device.worker_host.hostname)
            logger.error('[%d] worker-hostname error: %s', job.id, msg)
            fail_job(job, fail_msg=msg)
            return None

        device_object = PipelineDevice(device_config, device.hostname)  # equivalent of the NewDevice in lava-dispatcher, without .yaml file.
        # FIXME: drop this nasty hack once 'target' is dropped as a parameter
        if 'target' not in device_object:
            device_object.target = device.hostname
        device_object['hostname'] = device.hostname

    validate_list = job.sub_jobs_list if job.is_multinode else [job]
    for check_job in validate_list:
        parser_device = None if job.dynamic_connection else device_object
        try:
            logger.info("[%d] Parsing definition", check_job.id)
            # pass (unused) output_dir just for validation as there is no zmq socket either.
            pipeline_job = parser.parse(
                check_job.definition, parser_device,
                check_job.id, None, output_dir=check_job.output_dir)
        except (
                AttributeError, JobError, NotImplementedError,
                KeyError, TypeError, RuntimeError) as exc:
            logger.error('[%d] parser error: %s', check_job.id, exc)
            fail_job(check_job, fail_msg=exc)
            return None
        try:
            logger.info("[%d] Validating actions", check_job.id)
            pipeline_job.pipeline.validate_actions()
        except (AttributeError, JobError, KeyError, TypeError, RuntimeError) as exc:
            logger.error({device: exc})
            fail_job(check_job, fail_msg=exc)
            return None
        if pipeline_job:
            pipeline = pipeline_job.describe()
            # write the pipeline description to the job output directory.
            if not os.path.exists(check_job.output_dir):
                os.makedirs(check_job.output_dir)
            with open(os.path.join(check_job.output_dir, 'description.yaml'), 'w') as describe_yaml:
                describe_yaml.write(yaml.dump(pipeline))
            map_metadata(yaml.dump(pipeline), job)
            # add the compatibility result from the master to the definition for comparison on the slave.
            if 'compatibility' in pipeline:
                try:
                    compat = int(pipeline['compatibility'])
                except ValueError:
                    logger.error("[%d] Unable to parse job compatibility: %s",
                                 check_job.id, pipeline['compatibility'])
                    compat = 0
                check_job.pipeline_compatibility = compat
                check_job.save(update_fields=['pipeline_compatibility'])
            else:
                logger.error("[%d] Unable to identify job compatibility.", check_job.id)
                fail_job(check_job, fail_msg='Unknown compatibility')
                return None

    return device
