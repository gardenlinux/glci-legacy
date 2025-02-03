import logging
import typing

import binascii
import base64

import botocore.exceptions
import boto3.s3.transfer as bt
import botocore.client as client

import glci.aws
import glci.model as gm
import glci.util as gu

logger = logging.getLogger(__name__)


def check_blob_size_and_checksum(
        source_client: client,
        source_bucket: str,
        source_key: str,
        target_client: client,
        target_bucket: str,
        target_key: str
) -> bool:
    try:
        # there were cases where replicated blobs were corrupt (typically, they had
        # length of zero octets); as a (weak) validation, at least compare sizes
        # if sha256sums are available, we take those into account as well
        resp = target_client.head_object(
            Bucket=target_bucket,
            Key=target_key,
            ChecksumMode='ENABLED'
        )
        replicated_len = resp['ContentLength']
        replicated_sha256 = resp.get('ChecksumSHA256', None)
        if replicated_sha256 is not None:
            replicated_sha256 = binascii.hexlify(base64.b64decode(replicated_sha256))

        resp = source_client.head_object(
            Bucket=source_bucket,
            Key=source_key,
            ChecksumMode='ENABLED'
        )
        source_len = resp['ContentLength']
        source_sha256 = resp.get('ChecksumSHA256', None)
        if source_sha256 is not None:
            source_sha256 = binascii.hexlify(base64.b64decode(source_sha256))

        size_match = False
        checksum_match = False

        if replicated_len == source_len:
            logger.info(f"replicated blob sizes match: {source_len=}, {replicated_len=}")
            size_match = True
        else:
            logger.warning(f"replicated blob sizes do NOT match: {source_len=}, {replicated_len=}")

        if replicated_sha256 == source_sha256:
            logger.info(f"replicated checksums match: {replicated_sha256=}")
            checksum_match = True
        else:
            logger.warning(f"replicated SHA56 checksums do NOT match: {source_sha256=}, {replicated_sha256=}")

        return size_match and checksum_match
    except botocore.exceptions.ClientError as e:
        code = e.response['Error']['Code']
        if code == '404':
            logger.warning(f"replicated blob does not exist: {e}")
            return False
        else:
            raise e


def check_replicated_image_blobs(
    publishing_cfg: gm.PublishingCfg,
    release_manifests: typing.Iterable[gm.ReleaseManifest],
):
    source_bucket = publishing_cfg.origin_buildresult_bucket
    target_buckets = publishing_cfg.replica_buildresult_buckets

    s3_source_session = glci.aws.session(source_bucket.aws_cfg_name)
    s3_source_client = s3_source_session.client('s3')

    all_replicates_exist = True

    for target_bucket in target_buckets:
        logger.info(f'Checking image blob replication from {source_bucket.aws_cfg_name=} to {target_bucket.aws_cfg_name=}')
        s3_target_session = glci.aws.session(target_bucket.aws_cfg_name)
        s3_target_client = s3_target_session.client('s3')

        for manifest in release_manifests:
            if not manifest.platform in target_bucket.platforms:
                continue

            # hardcoded filtering: only replicate image-artefact (ignore anything else)
            suffix = gu.vm_image_artefact_for_platform(platform=manifest.platform)
            image_blob_ref =  manifest.path_by_suffix(suffix=suffix)

            logger.info(f"release artefact {image_blob_ref.s3_key}")
            replicate_exists = check_blob_size_and_checksum(s3_source_client,
                                source_bucket.bucket_name,
                                image_blob_ref.s3_key,
                                s3_target_client,
                                target_bucket.bucket_name,
                                image_blob_ref.s3_key
            )

            all_replicates_exist = all_replicates_exist and replicate_exists

    return all_replicates_exist
