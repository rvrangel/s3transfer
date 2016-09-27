# Copyright 2016 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import copy
import os
import tempfile
import shutil
import glob

from botocore.exceptions import ClientError

from tests import StreamWithError
from tests import FileSizeProvider
from tests import RecordingSubscriber
from tests import RecordingOSUtils
from tests import NonSeekableWriter
from tests import BaseGeneralInterfaceTest
from s3transfer.compat import six
from s3transfer.compat import SOCKET_ERROR
from s3transfer.exceptions import RetriesExceededError
from s3transfer.manager import TransferManager
from s3transfer.manager import TransferConfig
from s3transfer.download import GetObjectTask


class BaseDownloadTest(BaseGeneralInterfaceTest):
    def setUp(self):
        super(BaseDownloadTest, self).setUp()
        self.config = TransferConfig(max_request_concurrency=1)
        self._manager = TransferManager(self.client, self.config)

        # Create a temporary directory to write to
        self.tempdir = tempfile.mkdtemp()
        self.filename = os.path.join(self.tempdir, 'myfile')

        # Initialize some default arguments
        self.bucket = 'mybucket'
        self.key = 'mykey'
        self.extra_args = {}
        self.subscribers = []

        # Create a stream to read from
        self.content = b'my content'
        self.stream = six.BytesIO(self.content)

    def tearDown(self):
        super(BaseDownloadTest, self).tearDown()
        shutil.rmtree(self.tempdir)

    @property
    def manager(self):
        return self._manager

    @property
    def method(self):
        return self.manager.download

    def create_call_kwargs(self):
        return {
            'bucket': self.bucket,
            'key': self.key,
            'fileobj': self.filename
        }

    def create_invalid_extra_args(self):
        return {
            'Foo': 'bar'
        }

    def create_stubbed_responses(self):
        # We want to make sure the beginning of the stream is always used
        # incase this gets called twice.
        self.stream.seek(0)
        return [
            {
                'method': 'head_object',
                'service_response': {
                    'ContentLength': len(self.content)
                }
            },
            {
                'method': 'get_object',
                'service_response': {
                    'Body': self.stream
                }
            }
        ]

    def create_expected_progress_callback_info(self):
        # Note that last read is from the empty sentinel indicating
        # that the stream is done.
        return [
            {'bytes_transferred': 10}
        ]

    def add_head_object_response(self, expected_params=None):
        head_response = self.create_stubbed_responses()[0]
        if expected_params:
            head_response['expected_params'] = expected_params
        self.stubber.add_response(**head_response)

    def add_successful_get_object_responses(
            self, expected_params=None, expected_ranges=None):
        # Add all get_object responses needed to complete the download.
        # Should account for both ranged and nonranged downloads.
        for i, stubbed_response in enumerate(
                self.create_stubbed_responses()[1:]):
            if expected_params:
                stubbed_response['expected_params'] = copy.deepcopy(
                    expected_params)
                if expected_ranges:
                    stubbed_response['expected_params'][
                        'Range'] = expected_ranges[i]
            self.stubber.add_response(**stubbed_response)

    def add_n_retryable_get_object_responses(self, n, num_reads=0):
        for _ in range(n):
            self.stubber.add_response(
                method='get_object',
                service_response={
                    'Body': StreamWithError(
                        copy.deepcopy(self.stream), SOCKET_ERROR, num_reads)
                }
            )

    def test_download_temporary_file_does_not_exist(self):
        self.add_head_object_response()
        self.add_successful_get_object_responses()

        future = self.manager.download(**self.create_call_kwargs())
        future.result()
        # Make sure the file exists
        self.assertTrue(os.path.exists(self.filename))
        # Make sure the random temporary file does not exist
        possible_matches = glob.glob('%s*' % self.filename + os.extsep)
        self.assertEqual(possible_matches, [])

    def test_download_for_fileobj(self):
        self.add_head_object_response()
        self.add_successful_get_object_responses()

        with open(self.filename, 'wb') as f:
            future = self.manager.download(
                self.bucket, self.key, f, self.extra_args)
            future.result()

        # Ensure that the contents are correct
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())

    def test_download_for_seekable_filelike_obj(self):
        self.add_head_object_response()
        self.add_successful_get_object_responses()

        # Create a file-like object to test. In this case, it is a BytesIO
        # object.
        bytes_io = six.BytesIO()

        future = self.manager.download(
            self.bucket, self.key, bytes_io, self.extra_args)
        future.result()

        # Ensure that the contents are correct
        bytes_io.seek(0)
        self.assertEqual(self.content, bytes_io.read())

    def test_download_for_nonseekable_filelike_obj(self):
        self.add_head_object_response()
        self.add_successful_get_object_responses()

        with open(self.filename, 'wb') as f:
            future = self.manager.download(
                self.bucket, self.key, NonSeekableWriter(f), self.extra_args)
            future.result()

        # Ensure that the contents are correct
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())

    def test_download_cleanup_on_failure(self):
        self.add_head_object_response()

        # Throw an error on the download
        self.stubber.add_client_error('get_object')

        future = self.manager.download(**self.create_call_kwargs())

        with self.assertRaises(ClientError):
            future.result()
        # Make sure the actual file and the temporary do not exist
        # by globbing for the file and any of its extensions
        possible_matches = glob.glob('%s*' % self.filename)
        self.assertEqual(possible_matches, [])

    def test_download_with_nonexistent_directory(self):
        self.add_head_object_response()
        self.add_successful_get_object_responses()

        call_kwargs = self.create_call_kwargs()
        call_kwargs['fileobj'] = os.path.join(
            self.tempdir, 'missing-directory', 'myfile')
        future = self.manager.download(**call_kwargs)
        with self.assertRaises(IOError):
            future.result()

    def test_retries_and_succeeds(self):
        self.add_head_object_response()
        # Insert a response that will trigger a retry.
        self.add_n_retryable_get_object_responses(1)
        # Add the normal responses to simulate the download proceeding
        # as normal after the retry.
        self.add_successful_get_object_responses()

        future = self.manager.download(**self.create_call_kwargs())
        future.result()

        # The retry should have been consumed and the process should have
        # continued using the successful responses.
        self.stubber.assert_no_pending_responses()
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())

    def test_retry_failure(self):
        self.add_head_object_response()

        max_retries = 3
        self.config.num_download_attempts = max_retries
        self._manager = TransferManager(self.client, self.config)
        # Add responses that fill up the maximum number of retries.
        self.add_n_retryable_get_object_responses(max_retries)

        future = self.manager.download(**self.create_call_kwargs())

        # A retry exceeded error should have happened.
        with self.assertRaises(RetriesExceededError):
            future.result()

        # All of the retries should have been used up.
        self.stubber.assert_no_pending_responses()

    def test_retry_rewinds_callbacks(self):
        self.add_head_object_response()
        # Insert a response that will trigger a retry after one read of the
        # stream has been made.
        self.add_n_retryable_get_object_responses(1, num_reads=1)
        # Add the normal responses to simulate the download proceeding
        # as normal after the retry.
        self.add_successful_get_object_responses()

        recorder_subscriber = RecordingSubscriber()
        # Set the streaming to a size that is smaller than the data we
        # currently provide to it to simulate rewinds of callbacks.
        self.config.io_chunksize = 3
        future = self.manager.download(
            subscribers=[recorder_subscriber], **self.create_call_kwargs())
        future.result()

        # Ensure that there is no more remaining responses and that contents
        # are correct.
        self.stubber.assert_no_pending_responses()
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())

        # Assert that the number of bytes seen is equal to the length of
        # downloaded content.
        self.assertEqual(
            recorder_subscriber.calculate_bytes_seen(), len(self.content))

        # Also ensure that the second progress invocation was negative three
        # becasue a retry happened on the second read of the stream and we
        # know that the chunk size for each read is 3.
        progress_byte_amts = [
            call['bytes_transferred'] for call in
            recorder_subscriber.on_progress_calls
        ]
        self.assertEqual(-3, progress_byte_amts[1])

    def test_can_provide_file_size(self):
        self.add_successful_get_object_responses()

        call_kwargs = self.create_call_kwargs()
        call_kwargs['subscribers'] = [FileSizeProvider(len(self.content))]

        future = self.manager.download(**call_kwargs)
        future.result()

        # The HeadObject should have not happened and should have been able
        # to successfully download the file.
        self.stubber.assert_no_pending_responses()
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())

    def test_uses_provided_osutil(self):
        osutil = RecordingOSUtils()
        # Use the recording os utility for the transfer manager
        self._manager = TransferManager(self.client, self.config, osutil)

        self.add_head_object_response()
        self.add_successful_get_object_responses()

        future = self.manager.download(**self.create_call_kwargs())
        future.result()
        # The osutil should have had its open() method invoked when opening
        # a temporary file and its rename_file() method invoked when the
        # the temporary file was moved to its final location.
        self.assertEqual(len(osutil.open_records), 1)
        self.assertEqual(len(osutil.rename_records), 1)


class TestNonRangedDownload(BaseDownloadTest):
    # TODO: If you want to add tests outside of this test class and still
    # subclass from BaseDownloadTest you need to set ``__test__ = True``. If
    # you do not, your tests will not get picked up by the test runner! This
    # needs to be done until we find a better way to ignore running test cases
    # from the general test base class, which we do not want ran.
    __test__ = True

    def test_download(self):
        self.extra_args['RequestPayer'] = 'requester'
        expected_params = {
            'Bucket': self.bucket,
            'Key': self.key,
            'RequestPayer': 'requester'
        }
        self.add_head_object_response(expected_params)
        self.add_successful_get_object_responses(expected_params)
        future = self.manager.download(
            self.bucket, self.key, self.filename, self.extra_args)
        future.result()

        # Ensure that the contents are correct
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())

    def test_allowed_copy_params_are_valid(self):
        op_model = self.client.meta.service_model.operation_model('GetObject')
        for allowed_upload_arg in self._manager.ALLOWED_DOWNLOAD_ARGS:
            self.assertIn(allowed_upload_arg, op_model.input_shape.members)


class TestRangedDownload(BaseDownloadTest):
    # TODO: If you want to add tests outside of this test class and still
    # subclass from BaseDownloadTest you need to set ``__test__ = True``. If
    # you do not, your tests will not get picked up by the test runner! This
    # needs to be done until we find a better way to ignore running test cases
    # from the general test base class, which we do not want ran.
    __test__ = True

    def setUp(self):
        super(TestRangedDownload, self).setUp()
        self.config = TransferConfig(
            max_request_concurrency=1, multipart_threshold=1,
            multipart_chunksize=4)
        self._manager = TransferManager(self.client, self.config)

    def create_stubbed_responses(self):
        return [
            {
                'method': 'head_object',
                'service_response': {
                    'ContentLength': len(self.content)
                }
            },
            {
                'method': 'get_object',
                'service_response': {
                    'Body': six.BytesIO(self.content[0:4])
                }
            },
            {
                'method': 'get_object',
                'service_response': {
                    'Body': six.BytesIO(self.content[4:8])
                }
            },
            {
                'method': 'get_object',
                'service_response': {
                    'Body': six.BytesIO(self.content[8:])
                }
            }
        ]

    def create_expected_progress_callback_info(self):
        return [
            {'bytes_transferred': 4},
            {'bytes_transferred': 4},
            {'bytes_transferred': 2},
        ]

    def test_download(self):
        self.extra_args['RequestPayer'] = 'requester'
        expected_params = {
            'Bucket': self.bucket,
            'Key': self.key,
            'RequestPayer': 'requester'
        }
        expected_ranges = ['bytes=0-3', 'bytes=4-7', 'bytes=8-']
        self.add_head_object_response(expected_params)
        self.add_successful_get_object_responses(
            expected_params, expected_ranges)

        future = self.manager.download(
            self.bucket, self.key, self.filename, self.extra_args)
        future.result()

        # Ensure that the contents are correct
        with open(self.filename, 'rb') as f:
            self.assertEqual(self.content, f.read())
