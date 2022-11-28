import logging
import typing

import botocore.exceptions

import ccc.aws
import model

import glci.model as gm
import glci.util as gu

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
        s3_target_session = ccc.aws.session(target_bucket.aws_cfg_name)
        s3_target_client = s3_target_session.client('s3')

        for manifest in release_manifests:
            if not manifest.platform in target_bucket.platforms:
                continue

            # hardcoded filtering: only replicate image-artefact (ignore anything else)
            suffix = gu.virtual_image_artifact_for_platform(platform=manifest.platform)
            image_blob_ref =  manifest.path_by_suffix(suffix=suffix)

            try:
                resp = s3_target_client.head_object(
                    Bucket=target_bucket.bucket_name,
                    Key=image_blob_ref.s3_key,
                )
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


            # XXX: we _might_ split stream to multiple targets; however, as of now there is only
            # one single replication target, so skip this optimisation for now
            body = s3_source_client.get_object(
                Bucket=source_bucket.bucket_name,
                Key=image_blob_ref.s3_key,
            )['Body']


            logger.info(f'uploading to {target_bucket.bucket_name=}, {image_blob_ref.s3_key=}')
            logger.info('.. this may take a couple of minutes')
            s3_target_client.upload_fileobj(
                Fileobj=body,
                Bucket=target_bucket.bucket_name,
                Key=image_blob_ref.s3_key,
            )
