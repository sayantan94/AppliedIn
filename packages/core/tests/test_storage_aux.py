import boto3
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.queue import Queue
from appliedin_core.storage.secrets import SecretsClient


def test_artifact_roundtrip(aws):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="appliedin-test")
    store = ArtifactStore("appliedin-test", region="us-east-1")
    key = store.put("resumes", "acme-1.pdf", b"%PDF-1.7", "application/pdf")
    assert key == "resumes/acme-1.pdf"
    assert store.get(key) == b"%PDF-1.7"
    assert "resumes/acme-1.pdf" in store.presign(key)


def test_secrets_put_then_get(aws):
    client = SecretsClient(region="us-east-1")
    assert client.get_json("portal/acme") is None  # missing -> None, no raise
    client.put_json("portal/acme", {"user": "a", "password": "p"})
    assert client.get_json("portal/acme")["password"] == "p"


def test_queue_enqueue(aws):
    sqs = boto3.client("sqs", region_name="us-east-1")
    url = sqs.create_queue(QueueName="tailor")["QueueUrl"]
    mid = Queue(region="us-east-1").enqueue(url, {"pk": "acme#1"})
    assert mid
