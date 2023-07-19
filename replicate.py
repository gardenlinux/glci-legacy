import logging
import tempfile
import typing

import botocore.exceptions
import boto3.s3.transfer as bt

import ccc.aws
import model

import glci.model as gm
import glci.util as gu

from glci.aws import response_ok

logger = logging.getLogger(__name__)


def replicate_image_blobs(
    publishing_cfg: gm.PublishingCfg,
    release_manifests: typing.Iterable[gm.ReleaseManifest],
    cfg_factory: model.ConfigFactory,
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

            try:
                resp = s3_target_client.head_object(
                    Bucket=target_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                )

                # there were cases where replicated blobs were corrupt (typically, they had
                # length of zero octets); as a (weak) validation, at least compare sizes
                replicated_leng = resp['ContentLength']

                resp = s3_source_client.head_object(
                    Bucket=source_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                )
                if replicated_leng != (source_leng := resp['ContentLength']):
                    logger.warning(f'blob-sizes do not match: {replicated_leng=} {source_leng=}')
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
            except botocore.exceptions.ClientError as ce:
                print(ce)
                code = ce.response['Error']['Code']
                if code == '404':
                    pass # target does not exist yet - so replicate it
                else:
                    raise ce # do not hide other kinds of errors


            try:
                # XXX: we _might_ split stream to multiple targets; however, as of now there is only
                # one single replication target, so skip this optimisation for now
                resp = s3_source_client.get_object(
                    Bucket=source_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                )
                leng = resp['ContentLength']
                body = resp['Body']


                logger.info(f'uploading to {target_bucket.bucket_name=} for {target_bucket.aws_cfg_name=}, {image_blob_ref.s3_key=}')
                logger.info(f'.. this may take a couple of minutes ({leng} octets)')
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
                    response_ok(s3_source_client.download_fileobj(
                        Bucket=source_bucket.bucket_name,
                        Key=image_blob_ref.s3_key,
                        Fileobj=tf,
                    ))
                    logger.info('downloaded to tempfile - now starting to upload (2nd attempt)')
                    response_ok(s3_target_client.upload_fileobj(
                        Fileobj=body,
                        Bucket=target_bucket.bucket_name,
                        Key=image_blob_ref.s3_key,
                        Config=bt.TransferConfig(
                            num_download_attempts=20, # be very persistent before giving up
                            # rationale: we sometimes see "sporadic"
                            # connectivity issues when uploading through "great
                            # chinese firewall"
                            use_threads=True,
                            max_concurrency=5
                        ),
                    ))


            # make it world-readable (otherwise, vm-image-imports may fail)
            response_ok(s3_target_client.put_object_acl(
                ACL='public-read',
                Bucket=target_bucket.bucket_name,
                Key=image_blob_ref.s3_key,
            ))
