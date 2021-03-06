# Copyright (C) 2015 Linaro Limited
#
# Author: Stevan Radakovic <stevan.radakovic@linaro.org>
#
# This file is part of LAVA Server.
#
# LAVA Server is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License version 3
# as published by the Free Software Foundation
#
# LAVA Server is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with LAVA Server.  If not, see <http://www.gnu.org/licenses/>.

import csv
import io
import xmlrpclib
import yaml

from linaro_django_xmlrpc.models import ExposedAPI

from django.db.models.fields import FieldDoesNotExist

from lava_results_app.dbutils import export_testcase, testcase_export_fields
from lava_results_app.models import (
    Query,
    QueryCondition,
    RefreshLiveQueryError,
    TestCase,
    TestSuite,
    TestData,
    InvalidContentTypeError,
)
from lava_scheduler_app.models import TestJob


class ResultsAPI(ExposedAPI):

    def make_custom_query(self, entity, conditions):
        """
        Name
        ----
        `make_custom_query` (`entity`, `conditions`)

        Description
        -----------
        Construct and run a custom query and return the results.

        Arguments
        ---------
        `entity`: string
            The entity you want to query
        `conditions`: string
            The conditions of the query

        Return value
        ------------
        A list of dictionaries containing the query results.

        The user should be authenticated with a username and token.

        Example
        -------

        # Get all test jobs submitted by the user 'kernel-ci', and which ended as
        # 'Incomplete':
        server.results.make_custom_query("testjob",
            "testjob__submitter__exact__kernel-ci,"
            "testjob__status__exact__Incomplete")
        [{ jobXX }, { jobXY }, ...]

        # Get all test cases in a test suite named 'custom-tests', that failed,
        # and for whom the job ended after '2017-04-26 00:00:00'.
        server.results.make_custom_query("testcase",
            "testsuite__name__exact__1_custom-tests,"
            "testcase__result__exact__Test failed,"
            "testjob__end_time__gt__2017-04-26 00:00:00")
        [{ testcaseXX }, { testcaseXY }, ...]

        """
        self._authenticate()
        try:
            content_type = Query.get_content_type(entity)
        except InvalidContentTypeError:
            raise xmlrpclib.Fault(
                400, "Wrong table name in entity parameter. "
                "Please refer to query docs.")

        if content_type.model_class() not in QueryCondition.RELATION_MAP:
            raise xmlrpclib.Fault(
                400, "Wrong table name in entity parameter. "
                "Please refer to query docs.")

        conditions = Query.parse_conditions(content_type, conditions)

        try:
            results = Query.get_queryset(content_type, conditions).visible_by_user(
                self.user)
        except FieldDoesNotExist:
            raise xmlrpclib.Fault(400,
                                  "Conditions URL incorrect: Field does not exist. "
                                  "Please refer to query docs.")
        return list(results)

    def run_query(self, query_name, limit=200, username=None):
        """
        Name
        ----
        `run_query` (`query_name`, `limit=200`, `username=None`)

        Description
        -----------
        Run the specified query and return the results of the query.

        Arguments
        ---------
        `query_name`: string
            Query name string.
        `limit`: integer
            Add a limit to the number of results returned.
            Defaults to 200.
        `username`: string
            Username of the user which is owner of the query you would like the
            results.
            Defaults to None, in which case the method will consider the
            authenticated user to be the owner.
            Either way, the authenticated user needs to have special access to
            this query (being an owner or belonging to the group which has
            admin access to the query).

        Return value
        ------------
        A list of dictionaries containing the query results.

        The user should be authenticated with a username and token.
        """
        self._authenticate()
        if not username:
            username = self.user.username

        try:
            query = Query.objects.get(name=query_name,
                                      owner__username=username)
        except Query.DoesNotExist:
            raise xmlrpclib.Fault(
                404, "Query with name %s owned by user %s does not exist." %
                (query_name, username))

        if not query.is_accessible_by(self.user):
            raise xmlrpclib.Fault(
                403, "Permission denied for user to query %s" % query_name)

        return list(query.get_results(self.user, limit=limit))

    def refresh_query(self, query_name, username=None):
        """
        Name
        ----
        `refresh_query` (`query_name`, `username`)

        Description
        -----------
        Refreshes the query with the given name owned by specific user.

        Arguments
        ---------
        `query_name`: string
            Query name string.
        `username`: string
            Username of the user which is owner of/created the query you would
            like to update. Defaults to None, in which case the method will
            consider the authenticated user to be the owner.
            Either way, the authenticated user needs to have special access to
            this query (being an owner or belonging to the group which has
            admin access to the query).

        Return value
        ------------
        None. The user should be authenticated with a username and token.
        """
        self._authenticate()
        if not username:
            username = self.user.username

        try:
            query = Query.objects.get(name=query_name,
                                      owner__username=username)
        except Query.DoesNotExist:
            raise xmlrpclib.Fault(
                400, "Query with name %s owned by user %s does not exist." %
                (query_name, username))

        if not query.is_accessible_by(self.user):
            raise xmlrpclib.Fault(
                401, "Permission denied for user to query %s" % query_name)

        query.refresh_view()

    def refresh_all_queries(self):
        """
        Name
        ----
        `refresh_all_queries`

        Description
        -----------
        Refreshes all queries in the system. Available only for superusers.

        Arguments
        ---------
        None.

        Return value
        ------------
        None. The user should be authenticated with a username and token.
        """
        self._authenticate()

        if not self.user.is_superuser:
            raise xmlrpclib.Fault(
                401, "Permission denied for user %s. Must be a superuser to "
                "refresh all queries." % self.user.username)

        for query in Query.objects.all():
            try:
                query.refresh_view()
            except RefreshLiveQueryError:
                pass

    def get_testjob_results_yaml(self, job_id):
        """
        Name
        ----
        `get_testjob_results_yaml` (`job_id`)

        Description
        -----------
        Get the job results of given job id in the YAML format.

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of job results in YAML
        format, provided the user is authenticated with an username and token.
        """

        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)
            yaml_list = []
            for test_suite in job.testsuite_set.all():
                for test_case in test_suite.testcase_set.all():
                    yaml_list.append(export_testcase(test_case))

        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")

        return yaml.dump(yaml_list)

    def get_testjob_metadata(self, job_id):
        """
        Name
        ----
        `get_testjob_metadata` (`job_id`)

        Description
        -----------
        Get the job level metadata which includes entries created by
        LAVA as well as submitted in the test job definition

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of job results as
        a list of dictionaries, provided the user is authenticated with
        a username and token.

        [
            {name: value},
            {name: value},
        ]

        For example:
        [
            {'boot.0.hikey-oe.commands': 'fastboot'},
            {'source': 'https://git.linaro.org/lava-team/refactoring.git'},
            {'test.0.tlxc.definition.path': 'ubuntu/smoke-tests-basic.yaml'}
        ]
        """
        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)
        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")

        return job.get_metadata_dict()

    def get_testjob_results_csv(self, job_id):
        """
        Name
        ----
        `get_testjob_results_csv` (`job_id`)

        Description
        -----------
        Get the job results of given job id in CSV format.

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of job results in CSV
        format, provided the user is authenticated with an username and token.
        """

        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)
            output = io.BytesIO()
            writer = csv.DictWriter(
                output,
                quoting=csv.QUOTE_ALL,
                extrasaction='ignore',
                fieldnames=testcase_export_fields())
            writer.writeheader()
            for test_suite in job.testsuite_set.all():
                for row in test_suite.testcase_set.all():
                    writer.writerow(export_testcase(row))

        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")

        return output.getvalue()

    def get_testsuite_results_yaml(self, job_id, suite_name):
        """
        Name
        ----
        `get_testsuite_results_yaml` (`job_id`, `suite_name`)

        Description
        -----------
        Get the suite results of given job id and suite name in YAML format.

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.
        `suite_name`: string
            Name of the suite for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of suite results in YAML
        format, provided the user is authenticated with an username and token.
        """

        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)
            yaml_list = []
            test_suite = job.testsuite_set.get(name=suite_name)
            for test_case in test_suite.testcase_set.all():
                yaml_list.append(export_testcase(test_case))

        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")
        except TestSuite.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified test suite not found.")

        return yaml.dump(yaml_list)

    def get_testsuite_results_csv(self, job_id, suite_name):
        """
        Name
        ----
        `get_testsuite_results_csv` (`job_id`, `suite_name`)

        Description
        -----------
        Get the suite results of given job id and suite name in CSV format.

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.
        `suite_name`: string
            Name of the suite for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of suite results in CSV
        format, provided the user is authenticated with an username and token.
        """

        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)
            output = io.BytesIO()
            writer = csv.DictWriter(
                output,
                quoting=csv.QUOTE_ALL,
                extrasaction='ignore',
                fieldnames=testcase_export_fields())
            writer.writeheader()
            test_suite = job.testsuite_set.get(name=suite_name)
            for row in test_suite.testcase_set.all():
                writer.writerow(export_testcase(row))

        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")
        except TestSuite.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified test suite not found.")

        return output.getvalue()

    def get_testcase_results_yaml(self, job_id, suite_name, case_name):
        """
        Name
        ----
        `get_testcase_results_yaml` (`job_id`, `suite_name`, `case_name`)

        Description
        -----------
        Get the test case results of given job id, suite and test case name
        in YAML format.

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.
        `suite_name`: string
            Name of the suite for which the results are required.
        `case_name`: string
            Name of the test case for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of test case results in
        YAML format, provided the user is authenticated with an username and
        token.
        """

        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)
            test_suite = job.testsuite_set.get(name=suite_name)
            test_case = test_suite.testcase_set.get(name=case_name)
            yaml_list = [export_testcase(test_case)]

        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")
        except TestSuite.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified test suite not found.")
        except TestCase.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified test case not found.")

        return yaml.dump(yaml_list)

    def get_testcase_results_csv(self, job_id, suite_name, case_name):
        """
        Name
        ----
        `get_testcase_results_csv` (`job_id`, `suite_name`, `case_name`)

        Description
        -----------
        Get the test case results of given job id, suite and test case name
        in CSV format.

        Arguments
        ---------
        `job_id`: string
            Job id for which the results are required.
        `suite_name`: string
            Name of the suite for which the results are required.
        `case_name`: string
            Name of the test case for which the results are required.

        Return value
        ------------
        This function returns an XML-RPC structures of test case results in
        CSV format, provided the user is authenticated with an username and
        token.
        """

        self._authenticate()
        if not job_id:
            raise xmlrpclib.Fault(400, "Bad request: TestJob id was not "
                                  "specified.")
        try:
            job = TestJob.get_by_job_number(job_id)
            if not job.can_view(self.user):
                raise xmlrpclib.Fault(
                    401, "Permission denied for user to job %s" % job_id)

            output = io.BytesIO()
            writer = csv.DictWriter(
                output,
                quoting=csv.QUOTE_ALL,
                extrasaction='ignore',
                fieldnames=testcase_export_fields())
            writer.writeheader()
            test_suite = job.testsuite_set.get(name=suite_name)
            test_case = test_suite.testcase_set.get(name=case_name)
            writer.writerow(export_testcase(test_case))

        except TestJob.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified job not found.")
        except TestSuite.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified test suite not found.")
        except TestCase.DoesNotExist:
            raise xmlrpclib.Fault(404, "Specified test case not found.")

        return output.getvalue()
