""" Tests for API views. """

import json
from posixpath import join as urljoin
import uuid

import boto3
from celery import shared_task
from django.conf import settings
import ddt
from faker import Faker
from guardian.shortcuts import assign_perm
import mock
import moto
import requests
import responses
from rest_framework.test import APITestCase
from user_tasks.tasks import UserTask

from registrar.apps.api.tests.mixins import AuthRequestMixin
from registrar.apps.core import permissions as perms
from registrar.apps.core.jobs import (
    post_job_failure,
    post_job_success,
    start_job,
)
from registrar.apps.core.permissions import JOB_GLOBAL_READ
from registrar.apps.core.tests.factories import (
    OrganizationFactory,
    OrganizationGroupFactory,
    UserFactory,
)
from registrar.apps.core.tests.utils import mock_oauth_login
from registrar.apps.enrollments.tests.factories import ProgramFactory


class RegistrarAPITestCase(APITestCase):
    """ Base for tests of the Registrar API """

    api_root = '/api/v1/'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.edx_admin = UserFactory(username='edx-admin')
        assign_perm(perms.ORGANIZATION_READ_METADATA, cls.edx_admin)

        cls.stem_org = OrganizationFactory(name='STEM Institute')
        cls.cs_program = ProgramFactory(
            managing_organization=cls.stem_org,
            title="Master's in CS"
        )
        cls.mech_program = ProgramFactory(
            managing_organization=cls.stem_org,
            title="Master's in ME"
        )

        cls.stem_admin = UserFactory(username='stem-institute-admin')
        cls.stem_user = UserFactory(username='stem-institute-user')
        cls.stem_admin_group = OrganizationGroupFactory(
            organization=cls.stem_org,
            role=perms.OrganizationReadWriteEnrollmentsRole.name
        )
        cls.stem_user_group = OrganizationGroupFactory(
            organization=cls.stem_org,
            role=perms.OrganizationReadMetadataRole.name
        )
        cls.stem_admin.groups.add(cls.stem_admin_group)  # pylint: disable=no-member

        cls.hum_org = OrganizationFactory(name='Humanities College')
        cls.phil_program = ProgramFactory(
            managing_organization=cls.hum_org,
            title="Master's in Philosophy"
        )
        cls.english_program = ProgramFactory(
            managing_organization=cls.hum_org,
            title="Master's in English"
        )

        cls.hum_admin = UserFactory(username='humanities-college-admin')
        cls.hum_admin_group = OrganizationGroupFactory(
            organization=cls.hum_org,
            role=perms.OrganizationReadWriteEnrollmentsRole.name
        )
        cls.hum_admin.groups.add(cls.hum_admin_group)  # pylint: disable=no-member

    def mock_api_response(self, url, response_data, method='GET', response_code=200):
        responses.add(
            getattr(responses, method.upper()),
            url,
            body=json.dumps(response_data),
            content_type='application/json',
            status=response_code
        )


class S3MockMixin(object):
    """
    Mixin for classes that need to access S3 resources.

    Enables S3 mock and creates default bucket before tests.
    Disables S3 mock afterwards.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._s3_mock = moto.mock_s3()
        cls._s3_mock.start()
        conn = boto3.resource('s3')
        conn.create_bucket(Bucket=settings.AWS_STORAGE_BUCKET_NAME)

    @classmethod
    def tearDownClass(cls):
        cls._s3_mock.stop()
        super().tearDownClass()


@ddt.ddt
class ProgramListViewTests(RegistrarAPITestCase, AuthRequestMixin):
    """ Tests for the /api/v1/programs?org={org_key} endpoint """

    method = 'GET'
    path = 'programs'

    def test_all_programs(self):
        response = self.get('programs', self.edx_admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 4)

    def test_all_programs_unauthorized(self):
        response = self.get('programs', self.stem_admin)
        self.assertEqual(response.status_code, 403)

    @ddt.data(True, False)
    def test_list_programs(self, is_staff):
        user = self.edx_admin if is_staff else self.stem_admin
        response = self.get('programs?org=stem-institute', user)
        self.assertEqual(response.status_code, 200)
        response_programs = sorted(response.data, key=lambda p: p['program_key'])
        self.assertListEqual(
            response_programs,
            [
                {
                    'program_title': "Master's in CS",
                    'program_key': 'masters-in-cs',
                    'program_url':
                        'https://stem-institute.edx.org/masters-in-cs',
                },
                {
                    'program_title': "Master's in ME",
                    'program_key': 'masters-in-me',
                    'program_url':
                        'https://stem-institute.edx.org/masters-in-me',
                },
            ]
        )

    def test_list_programs_unauthorized(self):
        response = self.get('programs?org=stem-institute', self.hum_admin)
        self.assertEqual(response.status_code, 403)

    def test_org_not_found(self):
        response = self.get('programs?org=business-univ', self.stem_admin)
        self.assertEqual(response.status_code, 404)


@ddt.ddt
class ProgramRetrieveViewTests(RegistrarAPITestCase, AuthRequestMixin):
    """ Tests for the /api/v1/programs/{program_key} endpoint """

    method = 'GET'
    path = 'programs/masters-in-english'

    @ddt.data(True, False)
    def test_get_program(self, is_staff):
        user = self.edx_admin if is_staff else self.hum_admin
        response = self.get('programs/masters-in-english', user)
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(
            response.data,
            {
                'program_title': "Master's in English",
                'program_key': 'masters-in-english',
                'program_url':
                    'https://humanities-college.edx.org/masters-in-english',
            },
        )

    def test_get_program_unauthorized(self):
        response = self.get('programs/masters-in-english', self.stem_admin)
        self.assertEqual(response.status_code, 403)

    def test_program_not_found(self):
        response = self.get('programs/masters-in-polysci', self.stem_admin)
        self.assertEqual(response.status_code, 404)


@ddt.ddt
class ProgramCourseListViewTests(RegistrarAPITestCase, AuthRequestMixin):
    """ Tests for the /api/v1/programs/{program_key}/courses endpoint """

    method = 'GET'
    path = 'programs/masters-in-english/courses'

    @ddt.data(True, False)
    @mock_oauth_login
    @responses.activate
    def test_get_program_courses(self, is_staff):
        user = self.edx_admin if is_staff else self.hum_admin

        program_data = {
            'curricula': [
                {
                    'is_active': False,
                    'courses': []
                },
                {
                    'is_active': True,
                    'courses': [{
                        'course_runs': [
                            {
                                'key': '0001',
                                'uuid': '123456',
                                'title': 'Test Course 1',
                                'marketing_url': 'https://humanities-college.edx.org/masters-in-english/test-course-1',
                            }
                        ],
                    }]
                },
            ]
        }

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=program_data):
            response = self.get('programs/masters-in-english/courses', user)

        self.assertEqual(response.status_code, 200)
        self.assertListEqual(
            response.data,
            [{
                'course_id': '0001',
                'course_title': 'Test Course 1',
                'course_url': 'https://humanities-college.edx.org/masters-in-english/test-course-1',
            }],
        )

    def test_get_program_courses_unauthorized(self):
        response = self.get('programs/masters-in-cs/courses', self.hum_admin)
        self.assertEqual(response.status_code, 403)

    @mock_oauth_login
    @responses.activate
    def test_get_program_with_no_course_runs(self):
        user = self.hum_admin

        program_data = {
            'curricula': [{
                'is_active': True,
                'courses': [{
                    'course_runs': []
                }]
            }]
        }

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=program_data):
            response = self.get('programs/masters-in-english/courses', user)

        self.assertEqual(response.status_code, 200)
        self.assertListEqual(response.data, [])

    @mock_oauth_login
    @responses.activate
    def test_get_program_with_no_active_curriculum(self):
        user = self.hum_admin

        program_data = {
            'curricula': [{
                'is_active': False,
                'courses': [{
                    'course_runs': []
                }]
            }]
        }

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=program_data):
            response = self.get('programs/masters-in-english/courses', user)

        self.assertEqual(response.status_code, 200)
        self.assertListEqual(response.data, [])

    @mock_oauth_login
    @responses.activate
    def test_get_program_with_multiple_courses(self):
        user = self.stem_admin

        program_data = {
            'curricula': [{
                'is_active': True,
                'courses': [
                    {
                        'course_runs': [
                            {
                                'key': '0001',
                                'uuid': '0000-0001',
                                'title': 'Test Course 1',
                                'marketing_url': 'https://stem-institute.edx.org/masters-in-cs/test-course-1',
                            },
                        ],
                    },
                    {
                        'course_runs': [
                            {
                                'key': '0002a',
                                'uuid': '0000-0002a',
                                'title': 'Test Course 2',
                                'marketing_url': 'https://stem-institute.edx.org/masters-in-cs/test-course-2a',
                            },
                            {
                                'key': '0002b',
                                'uuid': '0000-0002b',
                                'title': 'Test Course 2',
                                'marketing_url': 'https://stem-institute.edx.org/masters-in-cs/test-course-2b',
                            },
                        ],
                    }
                ],
            }],
        }

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=program_data):
            response = self.get('programs/masters-in-cs/courses', user)

        self.assertEqual(response.status_code, 200)
        self.assertListEqual(
            response.data,
            [
                {
                    'course_id': '0001',
                    'course_title': 'Test Course 1',
                    'course_url': 'https://stem-institute.edx.org/masters-in-cs/test-course-1',
                },
                {
                    'course_id': '0002a',
                    'course_title': 'Test Course 2',
                    'course_url': 'https://stem-institute.edx.org/masters-in-cs/test-course-2a',
                },
                {
                    'course_id': '0002b',
                    'course_title': 'Test Course 2',
                    'course_url': 'https://stem-institute.edx.org/masters-in-cs/test-course-2b',
                }
            ],
        )

    def test_program_not_found(self):
        response = self.get('programs/masters-in-polysci/courses', self.stem_admin)
        self.assertEqual(response.status_code, 404)


class ProgramEnrollmentWriteMixin(object):
    """ Test write requests to the /api/v1/programs/{program_key}/enrollments endpoint """
    path = 'programs/masters-in-english/enrollments'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        program_uuid = cls.cs_program.discovery_uuid
        cls.lms_request_url = urljoin(
            settings.LMS_BASE_URL, 'api/program_enrollments/v1/programs/{}/enrollments/'
        ).format(program_uuid)

        cls.program_curriculum_data = {
            'curricula': [
                {'uuid': 'inactive-curriculum-0000', 'is_active': False},
                {'uuid': 'active-curriculum-0000', 'is_active': True}
            ]
        }

    def mock_enrollments_response(self, method, expected_response, response_code=200):
        self.mock_api_response(self.lms_request_url, expected_response, method=method, response_code=response_code)

    def student_enrollment(self, status, student_key=None):
        return {
            'status': status,
            'student_key': student_key or uuid.uuid4().hex[0:10]
        }

    def test_program_unauthorized_at_organization(self):
        req_data = [
            self.student_enrollment('enrolled'),
        ]

        response = self.request(self.method, 'programs/masters-in-cs/enrollments/', self.hum_admin, req_data)
        self.assertEqual(response.status_code, 403)

    def test_program_insufficient_permissions(self):
        req_data = [
            self.student_enrollment('enrolled'),
        ]
        response = self.request(self.method, 'programs/masters-in-cs/enrollments/', self.stem_user, req_data)
        self.assertEqual(response.status_code, 403)

    def test_program_not_found(self):
        req_data = [
            self.student_enrollment('enrolled'),
        ]
        response = self.request(
            self.method, 'programs/uan-salsa-dancing-with-sharks/enrollments/', self.stem_admin, req_data
        )
        self.assertEqual(response.status_code, 404)

    @mock_oauth_login
    @responses.activate
    def test_successful_program_enrollment_write(self):
        expected_lms_response = {
            '001': 'enrolled',
            '002': 'enrolled',
            '003': 'pending'
        }
        self.mock_enrollments_response(self.method, expected_lms_response)

        req_data = [
            self.student_enrollment('enrolled', '001'),
            self.student_enrollment('enrolled', '002'),
            self.student_enrollment('pending', '003'),
        ]

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=self.program_curriculum_data):
            response = self.request(self.method, 'programs/masters-in-cs/enrollments/', self.stem_admin, req_data)

        lms_request_body = json.loads(responses.calls[-1].request.body.decode('utf-8'))
        self.assertListEqual(lms_request_body, [
            {
                'status': 'enrolled',
                'student_key': '001',
                'curriculum_uuid': 'active-curriculum-0000'
            },
            {
                'status': 'enrolled',
                'student_key': '002',
                'curriculum_uuid': 'active-curriculum-0000'
            },
            {
                'status': 'pending',
                'student_key': '003',
                'curriculum_uuid': 'active-curriculum-0000'
            }
        ])
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(response.data, expected_lms_response)

    @mock_oauth_login
    @responses.activate
    def test_backend_unprocessable_response(self):
        self.mock_enrollments_response(self.method, "invalid enrollment record", response_code=422)

        req_data = [
            self.student_enrollment('enrolled', '001'),
            self.student_enrollment('enrolled', '002'),
            self.student_enrollment('pending', '003'),
        ]

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=self.program_curriculum_data):
            response = self.request(self.method, 'programs/masters-in-cs/enrollments/', self.stem_admin, req_data)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data, 'invalid enrollment record')

    @mock_oauth_login
    @responses.activate
    def test_backend_multi_status_response(self):
        expected_lms_response = {
            '001': 'enrolled',
            '002': 'enrolled',
            '003': 'invalid-status'
        }
        self.mock_enrollments_response(self.method, expected_lms_response, response_code=207)

        req_data = [
            self.student_enrollment('enrolled', '001'),
            self.student_enrollment('enrolled', '002'),
            self.student_enrollment('not_a_valid_value', '003'),
        ]

        with mock.patch('registrar.apps.api.v1.views.get_discovery_program', return_value=self.program_curriculum_data):
            response = self.request(self.method, 'programs/masters-in-cs/enrollments/', self.stem_admin, req_data)
        self.assertEqual(response.status_code, 207)
        self.assertDictEqual(response.data, expected_lms_response)

    def test_write_enrollment_payload_limit(self):
        req_data = [self.student_enrollment('enrolled')] * 26

        response = self.request(self.method, 'programs/masters-in-cs/enrollments/', self.stem_admin, req_data)
        self.assertEqual(response.status_code, 413)


class ProgramEnrollmentPostTests(ProgramEnrollmentWriteMixin, RegistrarAPITestCase, AuthRequestMixin):
    method = 'POST'


class ProgramEnrollmentPatchTests(ProgramEnrollmentWriteMixin, RegistrarAPITestCase, AuthRequestMixin):
    method = 'PATCH'


@ddt.ddt
class ProgramEnrollmentGetTests(S3MockMixin, RegistrarAPITestCase, AuthRequestMixin):
    """ Tests for GET /api/v1/programs/{program_key}/enrollments endpoint """
    method = 'GET'
    path = 'programs/masters-in-english/enrollments'

    enrollments = [
        {
            'student_key': 'abcd',
            'status': 'enrolled',
            'account_exists': True,
        },
        {
            'student_key': 'efgh',
            'status': 'pending',
            'account_exists': False,
        },
    ]
    enrollments_json = json.dumps(enrollments, indent=4)
    enrollments_csv = (
        "abcd,enrolled,true\n"
        "efgh,pending,false"
    )

    @mock.patch(
        'registrar.apps.enrollments.tasks.get_program_enrollments',
        return_value=enrollments,
    )
    @ddt.data(
        (None, 'json', enrollments_json),
        ('json', 'json', enrollments_json),
        ('csv', 'csv', enrollments_csv),
    )
    @ddt.unpack
    def test_ok(self, format_param, expected_format, expected_contents, _mock):
        format_suffix = "?fmt=" + format_param if format_param else ""
        response = self.get(self.path + format_suffix, self.hum_admin)
        self.assertEqual(response.status_code, 202)
        job_response = self.get(response.data['job_url'], self.hum_admin)
        self.assertEqual(job_response.status_code, 200)
        self.assertEqual(job_response.data['state'], 'Succeeded')

        result_url = job_response.data['result']
        self.assertIn(".{}?".format(expected_format), result_url)
        file_response = requests.get(result_url)
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.text, expected_contents)

    def test_permission_denied(self):
        response = self.get(self.path, self.stem_admin)
        self.assertEqual(response.status_code, 403)

    def test_program_not_found(self):
        response = self.get('programs/masters-in-polysci/courses', self.hum_admin)
        self.assertEqual(response.status_code, 404)


class JobStatusRetrieveViewTests(S3MockMixin, RegistrarAPITestCase, AuthRequestMixin):
    """ Tests for GET /api/v1/jobs/{job_id} endpoint """
    method = 'GET'
    path = 'jobs/a6393974-cf86-4e3b-a21a-d27e17932447'

    def test_successful_job(self):
        job_id = start_job(self.stem_admin, _succeeding_job)
        job_respose = self.get('jobs/' + job_id, self.stem_admin)
        self.assertEqual(job_respose.status_code, 200)

        job_status = job_respose.data
        self.assertIn('created', job_status)
        self.assertEqual(job_status['state'], 'Succeeded')
        result_url = job_status['result']
        self.assertIn("/job-results/{}.json?".format(job_id), result_url)

        file_response = requests.get(result_url)
        self.assertEqual(file_response.status_code, 200)
        json.loads(file_response.text)  # Make sure this doesn't raise an error

    @mock.patch('registrar.apps.core.jobs.logger', autospec=True)
    def test_failed_job(self, mock_jobs_logger):
        FAIL_MESSAGE = "everything is broken"
        job_id = start_job(self.stem_admin, _failing_job, FAIL_MESSAGE)
        job_respose = self.get('jobs/' + job_id, self.stem_admin)
        self.assertEqual(job_respose.status_code, 200)

        job_status = job_respose.data
        self.assertIn('created', job_status)
        self.assertEqual(job_status['state'], 'Failed')
        self.assertIsNone(job_status['result'])
        self.assertEqual(mock_jobs_logger.error.call_count, 1)

        error_logged = mock_jobs_logger.error.call_args_list[0][0][0]
        self.assertIn(job_id, error_logged)
        self.assertIn(FAIL_MESSAGE, error_logged)

    def test_job_permission_denied(self):
        job_id = start_job(self.stem_admin, _succeeding_job)
        job_respose = self.get('jobs/' + job_id, self.hum_admin)
        self.assertEqual(job_respose.status_code, 403)

    def test_job_global_read_permission(self):
        job_id = start_job(self.stem_admin, _succeeding_job)
        assign_perm(JOB_GLOBAL_READ, self.hum_admin)
        job_respose = self.get('jobs/' + job_id, self.hum_admin)
        self.assertEqual(job_respose.status_code, 200)

    def test_job_does_not_exist(self):
        nonexistant_job_id = str(uuid.uuid4())
        job_respose = self.get('jobs/' + nonexistant_job_id, self.stem_admin)
        self.assertEqual(job_respose.status_code, 404)


@shared_task(base=UserTask, bind=True)
def _succeeding_job(self, job_id, user_id):  # pylint: disable=unused-argument
    """ A job that just succeeds, posting an empty JSON list as its result. """
    fake_data = Faker().pystruct(20, str, int, bool)  # pylint: disable=no-member
    post_job_success(job_id, json.dumps(fake_data), 'json')


@shared_task(base=UserTask, bind=True)
def _failing_job(self, job_id, user_id, fail_message):  # pylint: disable=unused-argument
    """ A job that just fails, providing `fail_message` as its reason """
    post_job_failure(job_id, fail_message)
