#!/usr/bin/env python3

import logging

import functools
import dataclasses

import glci.aws
import glci.gcp
import glci.openstack_image
import glci.az
import glci.s3

import glci.model as gm
import glci.util

import ci.util

from glci.model import AwsPublishedImageSet

logger = logging.getLogger(__name__)

def cleanup_image(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool,
) -> gm.OnlineReleaseManifest:
    logger.info(f'cleaning up release for {release.platform=}')
    if dry_run:
        logger.warning(f"Running in DRY RUN mode {dry_run=}")

    if release.platform == 'ali':
        cleanup_function = cleanup_alicloud_images
    elif release.platform == 'aws':
        cleanup_function = cleanup_aws_images_by_id
    elif release.platform == 'gcp':
        cleanup_function = cleanup_gcp_images
    elif release.platform == 'azure':
        cleanup_function = cleanup_azure_community_gallery_images
    elif release.platform == 'openstack':
        cleanup_function = cleanup_openstack_images_by_id
    elif release.platform == 'openstackbaremetal':
        cleanup_function = cleanup_openstack_images_by_id
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
        raise


def cleanup_aws_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
    target_image_name = glci.aws.target_image_name_for_release(release=release)
    aws_publishing_cfg: gm.PublishingTargetAWS = publishing_cfg.target(platform=release.platform)

    for aws_cfg in aws_publishing_cfg.aws_cfgs:
        aws_cfg_name = aws_cfg.aws_cfg_name
        mk_session = functools.partial(glci.aws.session, aws_cfg=aws_cfg_name)
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
        mk_session = functools.partial(glci.aws.session, aws_cfg=aws_cfg_name)
        glci.aws.unregister_images_by_id(
            mk_session=mk_session,
            images=AwsPublishedImageSet(release.published_image_metadata.published_aws_images),
            dry_run=dry_run
        )


def cleanup_alicloud_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
    if dry_run:
        print(f'Aliyun cleanup dry run not implemented')
        return


    import glci.alicloud
    aliyun_cfg = publishing_cfg.target(release.platform)
    alicloud_cfg_name = aliyun_cfg.aliyun_cfg_name

    oss_auth = glci.alicloud.oss_auth(alicloud_cfg=alicloud_cfg_name)
    acs_client = glci.alicloud.acs_client(alicloud_cfg=alicloud_cfg_name)

    maker = glci.alicloud.AlicloudImageMaker(
        oss_auth,
        acs_client,
        release,
        aliyun_cfg,
    )

    maker.delete_images()


def cleanup_gcp_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
    gcp_publishing_cfg: gm.PublishingTargetGCP = publishing_cfg.target(release.platform)
    cfg_factory = ci.util.ctx().cfg_factory()
    gcp_cfg = cfg_factory.gcp(gcp_publishing_cfg.gcp_cfg_name)
    storage_client = glci.gcp.cloud_storage_client(gcp_cfg)
    compute_client = glci.gcp.authenticated_build_func(gcp_cfg)('compute', 'v1')

    glci.gcp.cleanup_image(
        storage_client=storage_client,
        compute_client=compute_client,
        gcp_project_name=gcp_cfg.project(),
        release=release,
        gcp_publishing_cfg=gcp_publishing_cfg,
        dry_run=dry_run
    )


def cleanup_openstack_images_by_id(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
    openstack_publishing_cfg: gm.PublishingTargetOpenstack = publishing_cfg.target(
        platform=release.platform,
    )

    cfg_factory = ci.util.ctx().cfg_factory()
    openstack_environments_cfg = cfg_factory.ccee(
        openstack_publishing_cfg.environment_cfg_name,
    )

    username = openstack_environments_cfg.credentials().username()
    password = openstack_environments_cfg.credentials().passwd()

    openstack_env_cfgs = {
        project.region(): gm.OpenstackEnvironment(
                project_name=project.name(),
                domain=project.domain(),
                region=project.region(),
                auth_url=project.auth_url(),
                username=username,
                password=password,
        ) for project in openstack_environments_cfg.projects()
    }

    published_images = release.published_image_metadata.published_openstack_images

    for image in published_images:
        openstack_env = openstack_env_cfgs.get(image.region_name, None)
        if not openstack_env:
            logger.error(f"Cannot remove image {image.image_id} because of missing OpenStack config for region {image.region_name}")
            continue

        glci.openstack_image.delete_single_image(
            openstack_environment_cfg=openstack_env,
            image_id=image.image_id,
            dry_run=dry_run
        )


def cleanup_openstack_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
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
        suffix=openstack_publishing_cfg.suffix,
        dry_run=dry_run
    )


def cleanup_azure_community_gallery_images(
    release: gm.OnlineReleaseManifest,
    publishing_cfg: gm.PublishingCfg,
    dry_run: bool = False
):
    cfg_factory = ci.util.ctx().cfg_factory()
    azure_publishing_cfgs: list[gm.PublishingTargetAzure] = publishing_cfg.target_multi(platform=release.platform)

    for azure_publishing_cfg in azure_publishing_cfgs:
        logger.info(f"targetting {azure_publishing_cfg.cloud}")

        azure_principal = cfg_factory.azure_service_principal(
            cfg_name=azure_publishing_cfg.service_principal_cfg_name,
        )

        azure_principal_serialized =  gm.AzureServicePrincipalCfg(
            tenant_id=azure_principal.tenant_id(),
            client_id=azure_principal.client_id(),
            client_secret=azure_principal.client_secret(),
            subscription_id=azure_principal.subscription_id(),
        )

        shared_gallery_cfg = cfg_factory.azure_shared_gallery(
            cfg_name=azure_publishing_cfg.gallery_cfg_name,
        )
        shared_gallery_cfg_serialized = gm.AzureSharedGalleryCfg(
            resource_group_name=shared_gallery_cfg.resource_group_name(),
            gallery_name=shared_gallery_cfg.gallery_name(),
            location=shared_gallery_cfg.location(),
            published_name=shared_gallery_cfg.published_name(),
            description=shared_gallery_cfg.description(),
            eula=shared_gallery_cfg.eula(),
            release_note_uri=shared_gallery_cfg.release_note_uri(),
            identifier_publisher=shared_gallery_cfg.identifier_publisher(),
            identifier_offer=shared_gallery_cfg.identifier_offer(),
            identifier_sku=shared_gallery_cfg.identifier_sku(),
            regions=azure_publishing_cfg.gallery_regions,
        )

        published_gallery_images = release.published_image_metadata.published_gallery_images

        for gallery_image in published_gallery_images:
            if gallery_image.azure_cloud != azure_publishing_cfg.cloud.value:
                continue

            glci.az.delete_from_azure_community_gallery(
                community_gallery_image_id=gallery_image.community_gallery_image_id,
                service_principal_cfg=azure_principal_serialized,
                shared_gallery_cfg=shared_gallery_cfg_serialized,
                azure_cloud=azure_publishing_cfg.cloud,
                dry_run=dry_run
            )
