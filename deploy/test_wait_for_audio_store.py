"""Run with the backend environment, which supplies boto3."""

import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, EndpointConnectionError

from wait_for_audio_store import wait_for_bucket


def storage_error(status):
    return ClientError(
        {
            "Error": {"Code": str(status)},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadBucket",
    )


class AudioStorageStartupTests(unittest.TestCase):
    @patch("wait_for_audio_store.time.sleep")
    def test_waits_for_bucket_creation_and_authenticated_access(self, sleep):
        client = Mock()
        client.head_bucket.side_effect = [
            EndpointConnectionError(endpoint_url="http://127.0.0.1:8333"),
            storage_error(404),
            storage_error(403),
            {},
        ]
        wait_for_bucket(client, "meeting-audio")
        self.assertEqual(client.head_bucket.call_count, 4)
        client.head_bucket.assert_called_with(Bucket="meeting-audio")
        self.assertEqual(sleep.call_count, 3)

    @patch("wait_for_audio_store.time.sleep")
    @patch("wait_for_audio_store.time.monotonic", side_effect=[0, 180])
    def test_bad_credentials_fail_with_an_actionable_message(self, clock, sleep):
        client = Mock()
        client.head_bucket.side_effect = storage_error(403)
        with self.assertRaisesRegex(RuntimeError, "credentials were rejected"):
            wait_for_bucket(client, "meeting-audio")
        sleep.assert_not_called()

    @patch("wait_for_audio_store.time.monotonic", side_effect=[0, 180])
    def test_unreachable_storage_fails_without_leaking_connection_details(self, clock):
        client = Mock()
        client.head_bucket.side_effect = EndpointConnectionError(
            endpoint_url="secret-detail"
        )
        with self.assertRaisesRegex(RuntimeError, "storage is unreachable") as result:
            wait_for_bucket(client, "meeting-audio")
        self.assertNotIn("secret-detail", str(result.exception))
