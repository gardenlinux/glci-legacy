import logging
import tempfile
import typing
import os

import botocore.exceptions
import boto3.s3.transfer as bt
import botocore.client as client

import ccc.aws
import model

import glci.model as gm
import glci.util as gu

from glci.aws import response_ok

logger = logging.getLogger(__name__)


def check_blob_sizes(
        source_client: client,
        source_bucket: str,
        source_key: str,
        target_client: client,
        target_bucket: str,
        target_key: str
) -> bool:
    try:
        checksum_not_available_default = 'checksum not available'

        # there were cases where replicated blobs were corrupt (typically, they had
        # length of zero octets); as a (weak) validation, at least compare sizes
        resp = target_client.head_object(
            Bucket=target_bucket,
            Key=target_key,
            ChecksumMode='ENABLED'
        )
        replicated_len = resp['ContentLength']
        replicated_sha256 = resp.get('ChecksumSHA256', checksum_not_available_default)

        resp = source_client.head_object(
            Bucket=source_bucket,
            Key=source_key,
            ChecksumMode='ENABLED'
        )
        source_len = resp['ContentLength']
        source_sha256 = resp.get('ChecksumSHA256', checksum_not_available_default)

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


def replicate_image_blobs(
    publishing_cfg: gm.PublishingCfg,
    release_manifests: typing.Iterable[gm.ReleaseManifest],
):
    source_bucket = publishing_cfg.origin_buildresult_bucket
    target_buckets = publishing_cfg.replica_buildresult_buckets

    s3_source_session = ccc.aws.session(source_bucket.aws_cfg_name)
    s3_source_client = s3_source_session.client('s3')

    for target_bucket in target_buckets:
        logger.info(f'Performing image blob replication from {source_bucket.aws_cfg_name=} to {target_bucket.aws_cfg_name=}')
        s3_target_session = ccc.aws.session(target_bucket.aws_cfg_name)
        s3_target_client = s3_target_session.client('s3')

        for manifest in release_manifests:
            if not manifest.platform in target_bucket.platforms:
                continue

            # hardcoded filtering: only replicate image-artefact (ignore anything else)
            suffix = gu.vm_image_artefact_for_platform(platform=manifest.platform)
            image_blob_ref =  manifest.path_by_suffix(suffix=suffix)

            if check_blob_sizes(s3_source_client,
                                source_bucket.bucket_name,
                                image_blob_ref.s3_key,
                                s3_target_client,
                                target_bucket.bucket_name,
                                image_blob_ref.s3_key
            ) is not True:
                logger.warning(f'will purge and re-replicate')
                s3_target_client.delete_object(
                    Bucket=target_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                )
            else:
                logger.info(
                    f'{image_blob_ref.s3_key} already existed in {target_bucket.bucket_name}'
                )
                continue

            image_size = 0
            try:
                # XXX: we _might_ split stream to multiple targets; however, as of now there is only
                # one single replication target, so skip this optimisation for now
                resp = s3_source_client.get_object(
                    Bucket=source_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                )
                image_size = resp['ContentLength']
                body = resp['Body']

                logger.info(f'streaming to {target_bucket.bucket_name=} for {target_bucket.aws_cfg_name=}, {image_blob_ref.s3_key=}')
                logger.info(f'.. this may take a couple of minutes ({image_size} octets)')
                s3_target_client.upload_fileobj(
                    Fileobj=body,
                    Bucket=target_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                    Config=bt.TransferConfig(
                        use_threads=True,
                        max_concurrency=5
                    ),
                )
            except Exception as e:
                logger.warning(f'there was an error trying to replicate using streaming: {e}')
                logger.info('falling back to tempfile-backed replication')

                with tempfile.TemporaryFile() as tf:
                    s3_source_client.download_fileobj(
                        Bucket=source_bucket.bucket_name,
                        Key=image_blob_ref.s3_key,
                        Fileobj=tf,
                    )
                    tempfile_size = tf.tell()

                    if tempfile_size != image_size:
                        raise RuntimeError(f"downloaded tempfile ({tempfile_size} octects) does not have the same size of the S3 image blob ({image_size} octects)")

                    logger.info(f"downloaded to tempfile ({tempfile_size=})")
                    tf.seek(0, os.SEEK_SET)

                    s3_target_client.upload_fileobj(
                        Fileobj=tf,
                        Bucket=target_bucket.bucket_name,
                        Key=image_blob_ref.s3_key,
                        Config=bt.TransferConfig(
                            use_threads=True,
                            max_concurrency=5,
                            num_download_attempts=20, # be very persistent before giving up
                            # rationale: we sometimes see "sporadic"
                            # connectivity issues when uploading through "great
                            # chinese firewall"
                        ),
                    )

            # check again that the transferred/replicated blob in the target has the same size as in the source, if this is not the case because of
            # errors in above upload_fileobj(), vm-image-imports will fail with misleading errors
            if check_blob_sizes(
                s3_source_client,
                source_bucket.bucket_name,
                image_blob_ref.s3_key,
                s3_target_client,
                target_bucket.bucket_name,
                image_blob_ref.s3_key
            ) is not True:
                raise RuntimeError(f"replicated blob sizes are not equal although replication operation did not yield any errors")

            # make it world-readable (otherwise, vm-image-imports may fail)
            response_ok(s3_target_client.put_object_acl(
                ACL='public-read',
                Bucket=target_bucket.bucket_name,
                Key=image_blob_ref.s3_key,
            ))
