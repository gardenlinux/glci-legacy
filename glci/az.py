import io

import dataclasses
from datetime import (
    datetime,
    timedelta,
    UTC,
)
import logging

from azure.mgmt.compute.v2023_07_03.models import (
    ImageVersionSecurityProfile,
    GalleryImageVersionUefiSettings,
    UefiKeySignatures
)
from azure.storage.blob import (
    BlobClient,
    BlobType,
    ContainerSasPermissions,
    generate_container_sas,
)

import glci.model
import glci.util
import version as version_util

# For Shared Image Gallery:
from azure.core.polling import LROPoller
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.compute.models import (
    CommunityGalleryImageVersion,
    GalleryArtifactVersionSource,
    GalleryImage,
    GalleryImageFeature,
    GalleryImageIdentifier,
    GalleryImageVersion,
    GalleryImageVersionPublishingProfile,
    GalleryImageVersionStorageProfile,
    HyperVGeneration,
    OperatingSystemStateTypes,
    OperatingSystemTypes,
    StorageAccountType,
    TargetRegion,
    UefiKey
)

from azure.mgmt.compute.models import Architecture as AzureArchitecture

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceNotFoundError,
)

logger = logging.getLogger(__name__)

# disable verbose http-logging from azure-sdk
logging.getLogger('azure.core.pipeline.policies.http_logging_policy').setLevel(logging.WARNING)


class AzureImageStore:
    """Azure Image Store backed by an container in an Azure Storage Account."""

    def __init__(
        self,
        storage_account_name: str,
        storage_account_key: str,
        container_name: str,
        storage_endpoint: str = "core.windows.net"
    ):
        self.sa_name = storage_account_name
        self.sa_key = storage_account_key
        self.container_name = container_name
        self.storage_endpoint = storage_endpoint

    def copy_from_s3(
        self,
        s3_client,
        s3_bucket_name: str,
        s3_object_key: str,
        target_blob_name: str
    ):
        """Copy an object from Amazon S3 to an Azure Storage Account

        This will overwrite the contents of the target file if it already exists.
        """
        connection_string = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={self.sa_name};"
            f"AccountKey={self.sa_key};"
            f"EndpointSuffix={self.storage_endpoint}"
        )
        image_blob = BlobClient.from_connection_string(
            conn_str=connection_string,
            container_name=self.container_name,
            blob_name=target_blob_name,
            blob_type=BlobType.PageBlob,
        )

        file_size_response = s3_client.head_object(Bucket=s3_bucket_name, Key=s3_object_key)
        file_size = file_size_response['ContentLength']

        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': s3_bucket_name, 'Key': s3_object_key},
        )

        image_blob.create_page_blob(file_size)
        # max size we can copy in one go is 4 mebibytes. Split the upload in steps with max size of
        # 4 MiB
        copy_step_length = 4 * 1024 * 1024
        offset = 0
        while offset < file_size:
            remaining = file_size - offset
            actual_cp_bytes = min(copy_step_length, remaining)

            image_blob.upload_pages_from_url(
                source_url=url,
                offset=offset,
                length=actual_cp_bytes,
                source_offset=offset,
            )
            offset += actual_cp_bytes

    def get_image_url(self, image_name: str):
        """Generate an url and an sas token to access image in the store and return both."""
        result_url = f'https://{self.sa_name}.blob.{self.storage_endpoint}/{self.container_name}/{image_name}'

        container_sas = generate_container_sas(
            account_name=self.sa_name,
            account_key=self.sa_key,
            container_name=self.container_name,
            permission=ContainerSasPermissions(read=True, list=True),
            start=datetime.now(UTC) - timedelta(days=1),
            expiry=datetime.now(UTC) + timedelta(days=30)
        )
        return result_url, container_sas


def copy_image_from_s3_to_az_storage_account(
    storage_account_cfg: glci.model.AzureStorageAccountCfg,
    s3_bucket_name: str,
    s3_object_key: str,
    target_blob_name: str,
    s3_client,
):
    """ copy object from s3 to storage account and return the generated access url including SAS token
    for the blob
    """
    if not target_blob_name.endswith('.vhd'):
        logger.warning(
            f"Destination image name '{target_blob_name}' does not end with '.vhd'! Resulting blob will "
            "not be suitable to create a marketplace offer from it!"
        )

    store = AzureImageStore(
        storage_account_name=storage_account_cfg.storage_account_name,
        storage_account_key=storage_account_cfg.access_key,
        container_name=storage_account_cfg.container_name,
        storage_endpoint=storage_account_cfg.endpoint_suffix
    )

    store.copy_from_s3(
        s3_client=s3_client,
        s3_bucket_name=s3_bucket_name,
        s3_object_key=s3_object_key,
        target_blob_name=target_blob_name,
    )

    return store.get_image_url(target_blob_name)


def _get_target_blob_name(release: glci.model.OnlineReleaseManifest, generation: glci.model.AzureHyperVGeneration = None) -> str:
    name = release.canonical_release_manifest_key_suffix()
    if generation and generation == glci.model.AzureHyperVGeneration.V2:
        return f"gardenlinux-{name}-gen2.vhd"
    return f"gardenlinux-{name}.vhd"


def _append_hyper_v_generation_architecture_and_secureboot(
        s: str,
        generation: glci.model.AzureHyperVGeneration,
        architecture: glci.model.Architecture,
        secureboot: bool
    ):
    if architecture == glci.model.Architecture.ARM64:
        s=f"{s}-arm64"
    if generation == glci.model.AzureHyperVGeneration.V2:
        s=f"{s}-gen2"
    if secureboot:
        s=f"{s}-secureboot"
    return s

def _create_shared_image(
    s3_client,
    cclient: ComputeManagementClient,
    sbclient: SubscriptionClient,
    shared_gallery_cfg: glci.model.AzureSharedGalleryCfg,
    resource_group_name: str,
    subscription_id: str,
    location: str,
    gallery_name: str,
    image_name: str,
    image_version: str,
    hyper_v_generation: glci.model.AzureHyperVGeneration,
    source_id: str,
    gallery_regions: list[str] | None,
    release: glci.model.OnlineReleaseManifest
) -> CommunityGalleryImageVersion:
    image_definition_name=_append_hyper_v_generation_architecture_and_secureboot(image_name, hyper_v_generation, release.architecture, release.secureboot)

    # begin_create_or_update() can change gallery image definitions - which is potentially dangerous for existing images
    # checking if a given gallery image definition already exists to make sure only new definitions get created
    # and existing definitions will not be touched
    logger.info(f'Creating gallery image definition {image_definition_name=}...')
    try:
        gallery_image = cclient.gallery_images.get(
            resource_group_name=resource_group_name,
            gallery_name=gallery_name,
            gallery_image_name=image_definition_name
        )
        logger.info(f'Gallery image definition {gallery_image.name} for generation {gallery_image.hyper_v_generation} on {gallery_image.architecture} already exists.')
    except ResourceNotFoundError:
        features = [
            GalleryImageFeature(name="IsAcceleratedNetworkSupported", value="True"),
            GalleryImageFeature(name="DiskControllerTypes", value="SCSI, NVMe"),
        ]
        if release.secureboot:
            features.append(GalleryImageFeature(name="SecurityType", value="TrustedLaunchSupported"))

        poller = cclient.gallery_images.begin_create_or_update(
            resource_group_name=resource_group_name,
            gallery_name=gallery_name,
            gallery_image_name=image_definition_name,
            gallery_image=GalleryImage(
                location=location,
                description=shared_gallery_cfg.description,
                eula=shared_gallery_cfg.eula,
                release_note_uri=shared_gallery_cfg.release_note_uri,
                features=features,
                os_type=OperatingSystemTypes.LINUX,
                os_state=OperatingSystemStateTypes.GENERALIZED,
                hyper_v_generation=HyperVGeneration(hyper_v_generation.value),
                architecture=AzureArchitecture.ARM64 if release.architecture == glci.model.Architecture.ARM64 else AzureArchitecture.X64,
                identifier=GalleryImageIdentifier(
                    publisher=shared_gallery_cfg.identifier_publisher,
                    offer=shared_gallery_cfg.identifier_offer,
                    sku=_append_hyper_v_generation_architecture_and_secureboot(shared_gallery_cfg.identifier_sku, hyper_v_generation, release.architecture, release.secureboot),
                )
            )
        )
        logger.info('...waiting for asynchronous operation to complete')
        poller.wait()

    regions = {
        l.name
        for l in sbclient.subscriptions.list_locations(subscription_id)
            if gallery_regions is None or l.name in gallery_regions
    }
    regions.add(shared_gallery_cfg.location) # ensure that the gallery's location is present

    # rm regions not yet supported (although they are returned by the subscription-client)
    regions -= {
        'brazilus',
        'jioindiacentral',
        'jioindiawest',
    }
    logger.info(f"gallery {regions=}")

    security_profile = None
    if release.secureboot:
        logger.info('retrieving secureboot certificates')
        buf = io.BytesIO()
        s3_client.download_fileobj(
            Bucket=release.s3_bucket,
            Key=release.path_by_suffix('.secureboot.pk.crt').s3_key,
            Fileobj=buf,
        )
        pk = buf.getvalue().decode()

        buf = io.BytesIO()
        s3_client.download_fileobj(
            Bucket=release.s3_bucket,
            Key=release.path_by_suffix('.secureboot.kek.crt').s3_key,
            Fileobj=buf,
        )
        kek = buf.getvalue().decode()

        buf = io.BytesIO()
        s3_client.download_fileobj(
            Bucket=release.s3_bucket,
            Key=release.path_by_suffix('.secureboot.db.crt').s3_key,
            Fileobj=buf,
        )
        db = buf.getvalue().decode()

        security_profile = ImageVersionSecurityProfile(
            uefi_settings=GalleryImageVersionUefiSettings(
                signature_template_names=[
                    "NoSignatureTemplate"
                ],
                additional_signatures=UefiKeySignatures(
                    pk=UefiKey(
                        type="x509",
                        value=[
                            pk
                        ]
                    ),
                    kek=[
                        UefiKey(
                            type="x509",
                            value=[
                                kek
                            ]
                        )
                    ],
                    db=[
                        UefiKey(
                            type="x509",
                            value=[
                                db
                            ]
                        )
                    ]
                )
            )
        )

    logger.info(f'Creating gallery image version {image_version=}')
    poller: LROPoller[GalleryImageVersion] = cclient.gallery_image_versions.begin_create_or_update(
        resource_group_name=resource_group_name,
        gallery_name=gallery_name,
        gallery_image_name=image_definition_name,
        gallery_image_version_name=image_version,
        gallery_image_version=GalleryImageVersion(
            location=location,
            tags={'component':'gardenlinux'},
            publishing_profile=GalleryImageVersionPublishingProfile(
                target_regions=[
                    TargetRegion(
                        name=r,
                        storage_account_type=StorageAccountType.STANDARD_LRS,
                        regional_replica_count=1
                    )
                    for r in regions
                ],
                replica_count=1,
                exclude_from_latest=False,
                # end_of_life_date=datetime.now() + timedelta(days=180),
                storage_account_type=StorageAccountType.STANDARD_LRS,
            ),
            storage_profile=GalleryImageVersionStorageProfile(
                source=GalleryArtifactVersionSource(
                    id=source_id
                )
            ),
            security_profile=security_profile
        )
    )
    logger.info('...waiting for asynchronous operation to complete')
    image_version: GalleryImageVersion = poller.result()

    # The creation above resulted in a GalleryImageVersion, which seems to be a supertype of both
    # Community Gallery images and Shared Gallery images and thus lacks information we need later.
    # Since there is no easy way to get the correct type and no direct connection to the Community
    # Gallery, fetch the CommunityGalleryImageVersion corresponding to the image we just created
    # and return it. It contains the proper "unique_id" we need to reference the shared image.
    # Note: Maybe in future there will be a
    # 'ComputeManagementClient.community_gallery_image_versions.begin_create_or_update()' function,
    # but as of now this seems be the way to go.

    gallery = cclient.galleries.get(
        resource_group_name=resource_group_name,
        gallery_name=gallery_name,
    )

    if not (public_gallery_name := next(
        iter(gallery.sharing_profile.community_gallery_info.public_names), None
    )):
        raise RuntimeError('Unable to determine the public gallery name for the published image.')

    community_gallery_image_version = cclient.community_gallery_image_versions.get(
        public_gallery_name=public_gallery_name,
        gallery_image_name=image_definition_name, # not obtainable from the created GalleryImageVersion
        gallery_image_version_name=image_version.name,  # yes, the name is the version since
                                                        # we have a GalleryImageVersion here
        location=image_version.location,
    )

    return community_gallery_image_version


def publish_to_azure_community_gallery(
    image_url: str,
    release: glci.model.OnlineReleaseManifest,
    published_version: str,
    hyper_v_generation: glci.model.AzureHyperVGeneration,
    cclient: ComputeManagementClient,
    sbclient: SubscriptionClient,
    subscription_id : str,
    shared_gallery_cfg: glci.model.AzureSharedGalleryCfg,
    azure_cloud: glci.model.AzureCloud,
    s3_client
) -> glci.model.AzureImageGalleryPublishedImage:
    published_name = _get_target_blob_name(release, hyper_v_generation)

    logger.info(f'Create community gallery image {published_name=} for Hyper-V generation {hyper_v_generation}')

    # Note: cclient.images.begin_create_or_update() can update an existing resource. However not all
    # properties can be updated. Especially updating image_url fails with an error.
    # Therefore it is safer to first delete the image if it exists than create it
    try:
        img_def = cclient.images.get(
            resource_group_name=shared_gallery_cfg.resource_group_name,
            image_name=published_name,
        )
        logger.info(f'Found existing image {img_def.id=}, {img_def.name=}. Delete it first')
        result = cclient.images.begin_delete(
            resource_group_name=shared_gallery_cfg.resource_group_name,
            image_name=published_name,
        )
        result = result.result()
        logger.info(f'Image deleted {result=}, will re-create now.')
    except ResourceNotFoundError:
        logger.info('Image does not exist will create it')

    result = cclient.images.begin_create_or_update(
            resource_group_name=shared_gallery_cfg.resource_group_name,
            image_name=published_name,
            parameters={
                'location': shared_gallery_cfg.location,
                'hyper_v_generation': HyperVGeneration(hyper_v_generation.value),
                'storage_profile': {
                    'os_disk': {
                        'os_type': 'Linux',
                        'os_state': 'Generalized',
                        'blob_uri': image_url,
                        'caching': 'ReadWrite',
                    }
                },
            }
    )
    logger.info('... waiting for operation to complete')
    result = result.result()
    logger.info(f'Image created: {result.id=}, {result.name=}, {result.type=}')

    shared_img = _create_shared_image(
        s3_client=s3_client,
        cclient=cclient,
        sbclient=sbclient,
        shared_gallery_cfg=shared_gallery_cfg,
        resource_group_name=shared_gallery_cfg.resource_group_name,
        subscription_id=subscription_id,
        location=shared_gallery_cfg.location,
        gallery_name=shared_gallery_cfg.gallery_name,
        image_name=shared_gallery_cfg.published_name,
        image_version=published_version,
        hyper_v_generation=hyper_v_generation,
        release=release,
        source_id=result.id,
        gallery_regions=shared_gallery_cfg.regions
    )

    unique_id = shared_img.unique_id
    logger.info(f'Image shared: {unique_id=}')

    community_gallery_published_image = glci.model.AzureImageGalleryPublishedImage(
        hyper_v_generation=hyper_v_generation.value,
        community_gallery_image_id=unique_id,
        azure_cloud=azure_cloud.value
    )

    return community_gallery_published_image


def publish_azure_image(
    s3_client,
    release: glci.model.OnlineReleaseManifest,
    service_principal_cfg: glci.model.AzureServicePrincipalCfg,
    storage_account_cfg: glci.model.AzureStorageAccountCfg,
    shared_gallery_cfg: glci.model.AzureSharedGalleryCfg,
    hyper_v_generations: list[glci.model.AzureHyperVGeneration],
    azure_cloud: glci.model.AzureCloud,
) -> glci.model.OnlineReleaseManifest:

    credential = ClientSecretCredential(
        tenant_id=service_principal_cfg.tenant_id,
        client_id=service_principal_cfg.client_id,
        client_secret=service_principal_cfg.client_secret,
        authority=azure_cloud.authority(),
    )

    # Copy image from s3 to Azure Storage Account
    azure_release_artifact = glci.util.vm_image_artefact_for_platform('azure')
    azure_release_artifact_path = release.path_by_suffix(azure_release_artifact)

    sclient = StorageManagementClient(credential, service_principal_cfg.subscription_id, base_url=azure_cloud.base_url(), credential_scopes=[azure_cloud.credential_scope()])
    cclient = ComputeManagementClient(credential, service_principal_cfg.subscription_id, base_url=azure_cloud.base_url(), credential_scopes=[azure_cloud.credential_scope()])
    sbclient = SubscriptionClient(credential, base_url=azure_cloud.base_url(), credential_scopes=[azure_cloud.credential_scope()])

    logger.info(f'using container name: {storage_account_cfg.container_name_sig=}')

    # prepare a blob container suitable for Shared Image Gallery
    try:
        sclient.blob_containers.create(
            resource_group_name=shared_gallery_cfg.resource_group_name,
            account_name=storage_account_cfg.storage_account_name,
            container_name=storage_account_cfg.container_name_sig,
            blob_container={
                'public_access': 'None'
            }
        )
    except ResourceExistsError:
        logger.info(f'Info: blob container {storage_account_cfg.container_name} already exists.')

    target_blob_name = _get_target_blob_name(release)

    logger.info(f'Copying from S3 (at {s3_client.meta.endpoint_url}) to Azure Storage Account blob: {target_blob_name=}')
    image_url, _ = copy_image_from_s3_to_az_storage_account(
        storage_account_cfg=storage_account_cfg,
        s3_client=s3_client,
        s3_bucket_name=azure_release_artifact_path.s3_bucket_name,  # FIXME: this must be adapted to the buildresult bucket, conicidence has it that they are both the same
        s3_object_key=azure_release_artifact_path.s3_key,
        target_blob_name=target_blob_name,
    )
    logger.info(f'copied from S3 to Azure Storage: {image_url=}')

    # version _must_ (of course..) be strict semver for azure
    published_version = str(version_util.parse_to_semver(release.version))

    # as we publish to different Azure Clouds {public, china}, we must preserve community gallery images
    # for those clouds we are not dealing with at the moment
    # even though we no longer support publishing to Az Marketplace, we need to preserve this code not
    # to mess up exsisting release manifests
    if release.published_image_metadata and release.published_image_metadata.published_marketplace_images:
        published_marketplace_images = release.published_image_metadata.published_marketplace_images
    else:
        published_marketplace_images = []

    if release.published_image_metadata and release.published_image_metadata.published_gallery_images:
        published_gallery_images = [
            cgimg for cgimg in release.published_image_metadata.published_gallery_images if cgimg.azure_cloud != azure_cloud.value
        ]
    else:
        published_gallery_images = []

    published_image = glci.model.AzurePublishedImage(
        published_marketplace_images=published_marketplace_images,
        published_gallery_images=published_gallery_images
    )

    for hyper_v_generation in hyper_v_generations:
        # arm64 requires Hyper-V gen2 therefore not publishing arm64 for gen1
        if hyper_v_generation == glci.model.AzureHyperVGeneration.V1 and release.architecture == glci.model.Architecture.ARM64:
            continue
        # secureboot requires Hyper-V gen2 therefore not publishing secureboot for gen1
        if hyper_v_generation == glci.model.AzureHyperVGeneration.V1 and release.secureboot:
            continue

        logger.info(f'Publishing community gallery image for {hyper_v_generation}...')
        gallery_published_image = publish_to_azure_community_gallery(
            image_url=image_url,
            release=release,
            published_version=published_version,
            hyper_v_generation=hyper_v_generation,
            cclient=cclient,
            sbclient=sbclient,
            subscription_id=service_principal_cfg.subscription_id,
            shared_gallery_cfg=shared_gallery_cfg,
            azure_cloud=azure_cloud,
            s3_client=s3_client
        )
        published_image.published_gallery_images.append(gallery_published_image)

    return dataclasses.replace(release, published_image_metadata=published_image)


def delete_from_azure_community_gallery(
    community_gallery_image_id: str,
    service_principal_cfg: glci.model.AzureServicePrincipalCfg,
    shared_gallery_cfg: glci.model.AzureSharedGalleryCfg,
    azure_cloud: glci.model.AzureCloud,
    dry_run: bool
):
    if dry_run:
        logger.warning(f"DRY RUN: would delete {community_gallery_image_id=}")
    else:
        logger.info(f"Deleting {community_gallery_image_id=}...")

    credential = ClientSecretCredential(
        tenant_id=service_principal_cfg.tenant_id,
        client_id=service_principal_cfg.client_id,
        client_secret=service_principal_cfg.client_secret,
        authority=azure_cloud.authority()
    )
    cclient = ComputeManagementClient(credential, service_principal_cfg.subscription_id, base_url=azure_cloud.base_url(), credential_scopes=[azure_cloud.credential_scope()])

    # unfortunately, it is not possible to obtain image information from its
    # community gallery image id through the API
    # so we have to dissect the string and apply some implicit knowledge about its structure
    gallery_image_id_parts = community_gallery_image_id.split('/')
    if len(gallery_image_id_parts) != 7:
        raise RuntimeError(f"community gallery image id {community_gallery_image_id} does not follow expected semantics")
    
    image_community_gallery_name = gallery_image_id_parts[2]
    image_definition = gallery_image_id_parts[4]
    image_version = gallery_image_id_parts[6]

    # check if the gallery names from the released artefact and the publishing cfg match
    configured_gallery = cclient.galleries.get(
        resource_group_name=shared_gallery_cfg.resource_group_name,
        gallery_name=shared_gallery_cfg.gallery_name
    )

    image_gallery_is_configured_gallery = False
    for public_name in configured_gallery.sharing_profile.community_gallery_info.public_names:
        if public_name == image_community_gallery_name:
            image_gallery_is_configured_gallery = True
            break

    if not image_gallery_is_configured_gallery:
        raise RuntimeError(f"The community gallery of image {community_gallery_image_id} is not from the configured community gallery {shared_gallery_cfg.gallery_name}.")

    gallery_image_version = cclient.gallery_image_versions.get(
        resource_group_name=shared_gallery_cfg.resource_group_name,
        gallery_name=shared_gallery_cfg.gallery_name,
        gallery_image_name=image_definition,
        gallery_image_version_name=image_version
    )

    # once again, resource group and image name has to be extracted from this string
    image_vhd = gallery_image_version.storage_profile.source.id
    image_vhd_parts = image_vhd.split('/')
    if len(image_vhd_parts) != 9:
        raise RuntimeError(f"image resource string {image_vhd} does not follow expected semantics")

    image_vhd_resource_group = image_vhd_parts[4]
    image_vhd_name = image_vhd_parts[8]

    if dry_run:
        logger.warning(f"DRY RUN: would delete gallery image version {gallery_image_version.name}")
        logger.warning(f"DRY RUN: would delete image VHD {image_vhd_name} in resource group {image_vhd_resource_group}")
    else:
        logger.info(f"Deleting {image_version=} for {image_definition=} in gallery {shared_gallery_cfg.gallery_name}...")
        result = cclient.gallery_image_versions.begin_delete(
            resource_group_name=shared_gallery_cfg.resource_group_name,
            gallery_name=shared_gallery_cfg.gallery_name,
            gallery_image_name=image_definition,
            gallery_image_version_name=image_version
        )
        logger.info('...waiting for asynchronous operation to complete')
        result.wait()
        
        logger.info(f"Deleting image VHD {image_vhd_name} in resource group {image_vhd_resource_group}...")
        result = cclient.images.begin_delete(
            resource_group_name=image_vhd_resource_group,
            image_name=image_vhd_name
        )
        logger.info('...waiting for asynchronous operation to complete')
        result.wait()

    # check how many image versions are present in this image definition
    # if none, that delete the image definition
    gallery_image_versions = cclient.gallery_image_versions.list_by_gallery_image(
        resource_group_name=shared_gallery_cfg.resource_group_name,
        gallery_name=shared_gallery_cfg.gallery_name,
        gallery_image_name=image_definition
    )

    image_version_count = sum(1 for _ in gallery_image_versions)
    if image_version_count == 0:
        if dry_run:
            logger.warning(f"DRY RUN: would delete {image_definition=} in gallery {shared_gallery_cfg.gallery_name}")
        else:
            logger.info(f"Deleting {image_definition=} in gallery {shared_gallery_cfg.gallery_name}...")
            result = cclient.gallery_images.begin_delete(
                resource_group_name=shared_gallery_cfg.resource_group_name,
                gallery_name=shared_gallery_cfg.gallery_name,
                gallery_image_name=image_definition
            )
            logger.info('...waiting for asynchronous operation to complete')
            result.wait()
    else:
        logger.warning(f"{image_definition=} still contains {image_version_count} image versions - keeping definition")
