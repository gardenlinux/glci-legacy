import dataclasses
import functools
import typing
import logging
from datetime import datetime
from time import sleep

from openstack import connect
import openstack.exceptions

import glci

logger = logging.getLogger(__name__)

class OpenstackImageUploader:
    '''OpenstackImageUploader is a client to upload images to Openstack Glance.'''

    def __init__(self, environment: glci.model.OpenstackEnvironment):
        self.openstack_env = environment

    @functools.lru_cache
    def _get_connection(self):
        return connect(
            auth_url=self.openstack_env.auth_url,
            project_name=self.openstack_env.project_name,
            username=self.openstack_env.username,
            password=self.openstack_env.password,
            region_name=self.openstack_env.region,
            user_domain_name=self.openstack_env.domain,
            project_domain_name=self.openstack_env.domain,
        )

    def upload_image_from_fs(self, name: str, path: str, meta: dict, timeout_seconds=86400):
        '''Upload an image from filesystem to Openstack Glance.'''

        conn = self._get_connection()
        image = conn.image.create_image(
            name=name,
            filename=path,
            disk_format='vmdk',
            container_format='bare',
            visibility='community',
            timeout=timeout_seconds,
            **meta,
        )
        return image['id']

    def delete_image(
        self,
        image_name: str,
    ):
        conn = self._get_connection()
        if (image_id := conn.image.find_image(name_or_id=image_name)):
            conn.image.delete_image(
                image=image_id,
            )

    def upload_image_from_url(self, name: str, url :str, meta: dict, timeout_seconds=86400):
        '''Import an image from web url to Openstack Glance.'''

        logger.info(
            f'Uploading image for region {self.openstack_env.region} '
            f'({self.openstack_env.project_name}) from {url} '
            f'with timeout of {timeout_seconds} seconds'
        )

        conn = self._get_connection()
        image = conn.image.create_image(
            name=name,
            disk_format='vmdk',
            container_format='bare',
            visibility='community',
            timeout=timeout_seconds,
            **meta,
        )
        conn.image.import_image(image, method="web-download", uri=url)
        return image['id']

    def wait_image_ready(self, image_id: str, wait_interval_seconds=10, timeout=3600):
        '''Wait until an image get in ready state.'''

        conn = self._get_connection()
        start_time = datetime.now()
        while True:
            if (datetime.now()-start_time).total_seconds() > timeout:
                raise RuntimeError(
                    f'Timeout for waiting image to get ready in {self.openstack_env.region} '
                    f'({self.openstack_env.project_name}) reached.'
                )
            image = conn.image.get_image(image_id)
            if image['status'] == 'queued' or image['status'] == 'saving' or image['status'] == 'importing':
                logger.info(
                    f'Image not yet ready in region {self.openstack_env.region} '
                    f'({self.openstack_env.project_name})'
                )
                sleep(wait_interval_seconds)
                continue
            if image['status'] == 'active':
                logger.info(f"Image is ready in region {self.openstack_env.region}: {image_id=}")
                return
            raise RuntimeError(
                f"Image upload to Glance failed in region {self.openstack_env.region} due "
                f"to image status {image['status']}"
            )


def upload_and_publish_image(
    s3_client,
    openstack_environments_cfgs: typing.Tuple[glci.model.OpenstackEnvironment],
    image_properties: dict,
    release: glci.model.OnlineReleaseManifest,
) -> glci.model.OnlineReleaseManifest:
    """Import an image from S3 into OpenStack Glance."""

    image_name = f"gardenlinux-{release.version}"
    image_meta = {
        'architecture': release.architecture.name,
        'properties': image_properties,
    }

    openstack_release_artifact = glci.util.vm_image_artefact_for_platform('openstack')
    openstack_release_artifact_path = release.path_by_suffix(openstack_release_artifact)

    s3_image_url = s3_client.generate_presigned_url(
        'get_object',
        ExpiresIn=1200*len(openstack_environments_cfgs), # 20min validity for each openstack enviroment/region
        Params={
            'Bucket': openstack_release_artifact_path.s3_bucket_name,
            'Key': openstack_release_artifact_path.s3_key,
        },
    )

    published_images = []
    for env_cfg in openstack_environments_cfgs:
        uploader = OpenstackImageUploader(env_cfg)
        image_id = uploader.upload_image_from_url(image_name, s3_image_url, image_meta)
        uploader.wait_image_ready(image_id)

        published_images.append(glci.model.OpenstackPublishedImage(
            region_name=env_cfg.region,
            image_id=image_id,
            image_name=image_name,
        ))

    published_image_set = glci.model.OpenstackPublishedImageSet(published_openstack_images=tuple(published_images))
    return dataclasses.replace(release, published_image_metadata=published_image_set)


def delete_images_for_release(
    openstack_environments_cfgs: typing.Tuple[glci.model.OpenstackEnvironment],
    release: glci.model.OnlineReleaseManifest,
) -> glci.model.OnlineReleaseManifest:
    """Delete all images created by a given release"""

    image_name = f"gardenlinux-{release.version}"
    for env_cfg in openstack_environments_cfgs:
        uploader = OpenstackImageUploader(env_cfg)
        uploader.delete_image(image_name=image_name)
