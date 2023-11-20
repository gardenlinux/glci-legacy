#!/usr/bin/env python3

import logging

import functools
import dataclasses
import os
import concurrent.futures
import datetime
import itertools
import typing

import glci.aws
import glci.gcp
import glci.model as gm
import glci.util

import ccc.aws
import ccc.gcp

import ci.util

logger = logging.getLogger(__name__)

def cleanup_image(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool,
) -> gm.OnlineReleaseManifest:
    logger.info(f'running release for {release.platform=}')

    if release.platform == 'ali':
        cleanup_function = None # clean_alicloud_images
    elif release.platform == 'aws':
        cleanup_function = cleanup_aws_images_by_id
    elif release.platform == 'gcp':
        cleanup_function = None # cleanup_gcp_images
    elif release.platform == 'azure':
        cleanup_function = None
    elif release.platform == 'openstack':
        cleanup_function = None # cleanup_openstack_images
    elif release.platform == 'oci':
        cleanup_function = None
    else:
        logger.warning(f'do not know how to clean up {release.platform=}, yet')
        return release

    try:
        cleanup_function(release, publishing_cfg, dry_run)
        if dry_run:
            return release
        else:
            return dataclasses.replace(release, published_image_metadata=None)
    except:
        import traceback
        traceback.print_exc()


def cleanup_aws_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
    target_image_name = glci.aws.target_image_name_for_release(release=release)
    aws_publishing_cfg: gm.PublishingTargetAWS = publishing_cfg.target(platform=release.platform)

    for aws_cfg in aws_publishing_cfg.aws_cfgs:
        aws_cfg_name = aws_cfg.aws_cfg_name
        mk_session = functools.partial(ccc.aws.session, aws_cfg=aws_cfg_name)
        glci.aws.unregister_images_by_name(
            mk_session=mk_session,
            image_name=target_image_name,
            dry_run=dry_run
        )


def cleanup_aws_images_by_id(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool
):
    aws_publishing_cfg: gm.PublishingTargetAWS = publishing_cfg.target(platform=release.platform)

    for aws_cfg in aws_publishing_cfg.aws_cfgs:
        aws_cfg_name = aws_cfg.aws_cfg_name
        mk_session = functools.partial(ccc.aws.session, aws_cfg=aws_cfg_name)
        glci.aws.unregister_images_by_id(
            mk_session=mk_session,
            images=release.published_image_metadata.published_aws_images,
            dry_run=dry_run
        )


def clean_alicloud_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
) -> gm.OnlineReleaseManifest:
    import ccc.alicloud
    import glci.alicloud
    aliyun_cfg = publishing_cfg.target(release.platform)
    alicloud_cfg_name = aliyun_cfg.aliyun_cfg_name

    oss_auth = ccc.alicloud.oss_auth(alicloud_cfg=alicloud_cfg_name)
    acs_client = ccc.alicloud.acs_client(alicloud_cfg=alicloud_cfg_name)

    maker = glci.alicloud.AlicloudImageMaker(
        oss_auth,
        acs_client,
        release,
        aliyun_cfg,
    )

    return maker.delete_images()


def cleanup_gcp_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
) -> gm.OnlineReleaseManifest:
    gcp_publishing_cfg: gm.PublishingTargetGCP = publishing_cfg.target(release.platform)
    cfg_factory = ci.util.ctx().cfg_factory()
    gcp_cfg = cfg_factory.gcp(gcp_publishing_cfg.gcp_cfg_name)
    storage_client = ccc.gcp.cloud_storage_client(gcp_cfg)
    compute_client = ccc.gcp.authenticated_build_func(gcp_cfg)('compute', 'v1')

    return glci.gcp.cleanup_image(
        storage_client=storage_client,
        compute_client=compute_client,
        gcp_project_name=gcp_cfg.project(),
        release=release,
        publishing_cfg=gcp_publishing_cfg,
    )


def cleanup_openstack_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
):
    import glci.openstack_image
    import ci.util

    openstack_publishing_cfg: gm.PublishingTargetOpenstack = publishing_cfg.target(
        platform=release.platform,
    )

    cfg_factory = ci.util.ctx().cfg_factory()
    openstack_environments_cfg = cfg_factory.ccee(
        openstack_publishing_cfg.environment_cfg_name,
    )

    username = openstack_environments_cfg.credentials().username()
    password = openstack_environments_cfg.credentials().passwd()

    openstack_env_cfgs = tuple((
        gm.OpenstackEnvironment(
            project_name=project.name(),
            domain=project.domain(),
            region=project.region(),
            auth_url=project.auth_url(),
            username=username,
            password=password,
        ) for project in openstack_environments_cfg.projects()
    ))

    glci.openstack_image.delete_images_for_release(
        openstack_environments_cfgs=openstack_env_cfgs,
        release=release,
    )


def clean_release_manifest_sets(
    max_age_days: int=14,
    cicd_cfg=None,
    prefix: str=os.path.join(
      gm.ReleaseManifestSet.release_manifest_set_prefix,
      gm.PipelineFlavour.SNAPSHOT.value,
    ),
    dry_run: bool=False,
):
    if not cicd_cfg:
        cicd_cfg = gm.CicdCfg=glci.util.cicd_cfg()
    enumerate_release_sets = glci.util.preconfigured(
        glci.util.enumerate_release_sets,
        cicd_cfg=cicd_cfg,
    )

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=64)

    now = datetime.datetime.now()
    oldest_allowed_date = now - datetime.timedelta(days=max_age_days)
    logger.debug(f'{oldest_allowed_date=}')

    s3_client = glci.s3.s3_client(cicd_cfg=cicd_cfg)

    def purge_if_outdated(release_manifest_set: gm.ReleaseManifestSet):
        if len(release_manifest_set.manifests) < 1:
            logger.warning(f'{release_manifest_set.s3_key=} did not contain any manifests')
            return (False, release_manifest_set)

        first_manifest = release_manifest_set.manifests[0]
        # all timestamps should usually be pretty close to each other

        if first_manifest.build_ts_as_date() > oldest_allowed_date:
            return (False, release_manifest_set)

        # XXX also purge published images (if any)!
        if dry_run:
            logger.info(
                f'Would delete {release_manifest_set.s3_bucket}/{release_manifest_set.s3_key}'
            )
            return (False, release_manifest_set)
        else:
            s3_client.delete_object(
                Bucket=release_manifest_set.s3_bucket,
                Key=release_manifest_set.s3_key,
            )
            logger.info(f'purged {release_manifest_set.s3_key=}')
            return (True, release_manifest_set)

    for purged, manifest in executor.map(
        purge_if_outdated,
        enumerate_release_sets(prefix=prefix)
    ):
        pass


def clean_single_release_manifests(
    max_age_days: int=14,
    cicd_cfg=None,
    prefix: str=gm.ReleaseManifest.manifest_key_prefix,
    dry_run: bool=False,
):
    if not cicd_cfg:
        cicd_cfg = gm.CicdCfg=glci.util.cicd_cfg()

    enumerate_releases = glci.util.preconfigured(
        glci.util.enumerate_releases,
        cicd_cfg=cicd_cfg,
    )

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=64)

    now = datetime.datetime.now()
    oldest_allowed_date = now - datetime.timedelta(days=max_age_days)
    logger.debug(f'{oldest_allowed_date=}')

    s3_client = glci.s3.s3_client(cicd_cfg=cicd_cfg)

    def purge_if_outdated(release_manifest: gm.ReleaseManifest):
        if release_manifest.build_ts_as_date() < oldest_allowed_date:
            # XXX also purge published images (if any)!
            if dry_run:
                logger.info(f'would delete {release_manifest.s3_bucket}/{release_manifest.s3_key}')
                return (False, release_manifest)
            else:
                s3_client.delete_object(
                    Bucket=release_manifest.s3_bucket,
                    Key=release_manifest.s3_key,
                )
                logger.info(f'purged {release_manifest.s3_key=}')
                return (True, release_manifest)
        return (False, release_manifest)

    for purged, manifest in executor.map(purge_if_outdated, enumerate_releases()):
        pass


def _enumerate_objects_from_single_release_manifests(
    cicd_cfg=None,
    prefix: str=gm.ReleaseManifest.manifest_key_prefix,
) -> typing.Generator[gm.S3_ReleaseFile, None, None]:
    if not cicd_cfg:
        cicd_cfg = gm.CicdCfg=glci.util.cicd_cfg()

    enumerate_releases = glci.util.preconfigured(
        glci.util.enumerate_releases,
        cicd_cfg=cicd_cfg,
    )
    for release_manifest in enumerate_releases(prefix=prefix):
        yield from release_manifest.paths


def _enumerate_objects_from_release_manifest_sets(
    cicd_cfg=None,
    prefix: str=gm.ReleaseManifestSet.release_manifest_set_prefix,
) -> typing.Generator[gm.S3_ReleaseFile, None, None]:
    if not cicd_cfg:
        cicd_cfg = gm.CicdCfg=glci.util.cicd_cfg()

    enumerate_release_sets = glci.util.preconfigured(
        glci.util.enumerate_release_sets,
        cicd_cfg=cicd_cfg,
    )

    for release_manifest_set in enumerate_release_sets(prefix=prefix):
        for release_manifest in release_manifest_set.manifests:
            yield from release_manifest.paths
        if release_manifest_set.logs:
            yield from release_manifest_set.logs


def clean_orphaned_objects(
    cicd_cfg=None,
    prefix='objects',
    dry_run: bool=False,
):
    if not cicd_cfg:
        cicd_cfg = gm.CicdCfg=glci.util.cicd_cfg()

    all_objects = {
        object_descriptor for object_descriptor in
        itertools.chain(
            _enumerate_objects_from_release_manifest_sets(
                cicd_cfg=cicd_cfg,
            ),
            _enumerate_objects_from_single_release_manifests(
                cicd_cfg=cicd_cfg,
            )
        )
    }

    # XXX assume for now that we only use one bucket
    s3_bucket_name = cicd_cfg.build.s3_bucket_name
    all_object_keys = {
        o.s3_key for o in all_objects if o.s3_bucket_name == s3_bucket_name
    }

    logger.debug(f'{len(all_objects)=}')
    logger.debug(f'{len(all_object_keys)=}')

    s3_client = glci.s3.s3_client(cicd_cfg=cicd_cfg)

    continuation_token = None
    while True:
        ctoken_args = {'ContinuationToken': continuation_token} \
                if continuation_token \
                else {}

        res = s3_client.list_objects_v2(
            Bucket=s3_bucket_name,
            Prefix=prefix,
            **ctoken_args,
        )
        if res['KeyCount'] == 0:
            break

        continuation_token = res.get('NextContinuationToken')

        object_keys = {obj_dict['Key'] for obj_dict in res['Contents']}

        # determine those keys that are no longer referenced by any manifest
        loose_object_keys = object_keys - all_object_keys

        if dry_run:
            logger.info(f'would delete {len(loose_object_keys)=} unreferenced objs:')

        else:
            if loose_object_keys:
                s3_client.delete_objects(
                    Bucket=s3_bucket_name,
                    Delete={
                    'Objects': [
                        {'Key': key} for key in loose_object_keys
                    ],
                    },
                )
                logger.info(f'purged {len(loose_object_keys)=} unreferenced objs')

        if not continuation_token:
          break
