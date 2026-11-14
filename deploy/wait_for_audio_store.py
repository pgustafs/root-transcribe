"""Wait for authenticated access to the audio bucket before starting app processes."""

import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def wait_for_bucket(client, bucket: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    reason = "storage is unreachable"
    while True:
        try:
            client.head_bucket(Bucket=bucket)
            return
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            reason = {
                403: "credentials were rejected",
                404: "the audio bucket does not exist",
            }.get(status, "storage returned an error")
        except BotoCoreError:
            reason = "storage is unreachable"
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Audio storage did not become ready: {reason}. "
                "Check SeaweedFS logs and the saved storage credentials."
            ) from None
        time.sleep(2)


def main() -> None:
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["AUDIO_S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["AUDIO_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["AUDIO_S3_SECRET_KEY"],
        region_name=os.environ.get("AUDIO_S3_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=3,
            retries={"total_max_attempts": 1},
        ),
    )
    print("Waiting for the audio bucket...", flush=True)
    try:
        wait_for_bucket(client, os.environ["AUDIO_S3_BUCKET"])
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        client.close()
    print("Audio storage is ready.", flush=True)


if __name__ == "__main__":
    main()
