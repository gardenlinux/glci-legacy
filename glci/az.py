import dataclasses
from datetime import (
    datetime,
    timedelta,
)
from enum import Enum
import logging
import typing

import requests
from glci import util
import version
from msal import ConfidentialClientApplication
from azure.storage.blob import (
    BlobClient,
    BlobType,
    ContainerSasPermissions,
    generate_container_sas,
)

import glci.model
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
    GalleryImageIdentifier,
    GalleryImageVersion,
    GalleryImageVersionPublishingProfile,
    GalleryImageVersionStorageProfile,
    HyperVGeneration,
    OperatingSystemStateTypes,
    OperatingSystemTypes,
    StorageAccountType,
    TargetRegion,
)

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceNotFoundError,
)

logger = logging.getLogger(__name__)

# disable verbose http-logging from azure-sdk
logging.getLogger('azure.core.pipeline.policies.http_logging_policy').setLevel(logging.WARNING)


'''
The publishing process for an image to the Azure Marketplace consist of
two sequences of steps.

1. publishing steps - this include the upload of the image to an Azure StorageAccount,
the update of the gardenlinux Marketplace spec, the trigger of the publish operation
which will trigger the validation of the image on the Microsoft side and upload
the image into their staging enviroment.
Those steps are covered by the "upload_and_publish_image" function.

2. check and approve steps – first the progress of the triggered publish operation
will be checked. If the publish operation has been completed the go live operation
will be triggered automatically. After that it will check for the progress of the
go live operation and if this also has been completed it will return the urn of the image.
Those steps are covered by the "check_offer_transport_state" function.
It need to be called multiple times until the entire process has been completed.
'''


class AzureImageStore:
    '''Azure Image Store backed by an container in an Azure Storage Account.'''

    def __init__(
        self,
        storage_account_name: str,
        storage_account_key: str,
        container_name: str
    ):
        self.sa_name = storage_account_name
        self.sa_key = storage_account_key
        self.container_name = container_name

    def copy_from_s3(
        self,
        s3_client,
        s3_bucket_name: str,
        s3_object_key: str,
        target_blob_name: str
    ):
        '''Copy an object from Amazon S3 to an Azure Storage Account

        This will overwrite the contents of the target file if it already exists.
        '''
        connection_string = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={self.sa_name};"
            f"AccountKey={self.sa_key};"
            "EndpointSuffix=core.windows.net"
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
        '''Generate an url and an sas token to access image in the store and return both.'''
        result_url = f'https://{self.sa_name}.blob.core.windows.net/{self.container_name}/{image_name}'

        container_sas = generate_container_sas(
            account_name=self.sa_name,
            account_key=self.sa_key,
            container_name=self.container_name,
            permission=ContainerSasPermissions(read=True, list=True),
            start=datetime.utcnow() - timedelta(days=1),
            expiry=datetime.utcnow() + timedelta(days=30)
        )
        return result_url, container_sas


class AzmpOperationState(Enum):
    NOTSTARETD = "notStarted"
    RUNNING = "running"
    COMPLETED = "completed"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    FAILED = "failed"

class AzmpTransportDest(Enum):
    STAGING = "staging"
    PROD = "production"

class AzureMarketplaceClient:
    '''Azure Marketplace Client is a client to interact with the Azure Marketplace.'''

    marketplace_baseurl = "https://cloudpartner.azure.com/api/publishers"

    def __init__(self, spn_tenant_id: str, spn_client_id: str, spn_client_secret: str):
        app_client = ConfidentialClientApplication(
            client_id=spn_client_id,
            authority=f"https://login.microsoftonline.com/{spn_tenant_id}",
            client_credential=spn_client_secret
        )
        token = app_client.acquire_token_for_client(scopes="https://cloudpartner.azure.com/.default")
        if 'error' in token:
            raise RuntimeError("Could not fetch token for Azure Marketplace client", token['error_description'])
        self.token = token['access_token']

    def _request(self, url: str, method='GET', headers={}, params={}, **kwargs):
        if 'Authorization' not in headers:
            headers['Authorization'] = f"Bearer {self.token}"
        if 'Content-Type' not in headers:
            headers['Content-Type'] = "application/json"

        if 'api-version' not in params:
            params['api-version'] = '2017-10-31'

        return requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            **kwargs
        )

    def _api_url(self, *parts):
        return '/'.join(p for p in (self.marketplace_baseurl, *parts))

    def _raise_for_status(self, response, message=""):
        if response.ok:
            return
        if response.status_code == 409:
            logger.warning(f"Conflicting Azure MP operation exists: {message}. statuscode={response.status_code}")
            return
        if message:
            raise RuntimeError(f"{message}. statuscode={response.status_code}")
        raise RuntimeError(f"HTTP call to {response.url} failed. statuscode={response.status_code}")

    def fetch_offer(self, publisher_id: str, offer_id: str):
        '''Fetch an offer from Azure marketplace.'''

        response = self._request(url=self._api_url(publisher_id, "offers", offer_id))
        self._raise_for_status(
            response=response,
            message='Fetching of Azure marketplace offer for gardenlinux failed',
        )
        offer_spec = response.json()
        return offer_spec

    def update_offer(self, publisher_id: str, offer_id: str, spec: dict):
        '''Update an offer with a give spec.'''

        response = self._request(
            url=self._api_url(publisher_id, "offers", offer_id),
            method='PUT',
            headers={"If-Match": "*"},
            json=spec,
        )
        self._raise_for_status(
            response=response,
            message='Update of Azure marketplace offer for gardenlinux failed',
        )

    def publish_offer(self, publisher_id: str, offer_id: str, notification_mails=()):
        '''Trigger (re-)publishing of an offer.'''

        data = {
            "metadata": {
                "notification-emails": ",".join(notification_mails)
            }
        }
        res = self._request(
            method='POST',
            url=self._api_url(publisher_id, 'offers', offer_id, 'publish'),
            json=data,
        )
        self._raise_for_status(
            response=res,
            message=f'{res=} {res.status_code=} {res.reason=} {res.content=}'
        )

    def fetch_ongoing_operation_id(self, publisher_id: str, offer_id: str, transport_dest: AzmpTransportDest):
        '''Fetches the id of an ongoing Azure Marketplace transport operation to a certain transport destination.'''

        response = self._request(url=self._api_url(publisher_id, "offers", offer_id, "submissions"))
        self._raise_for_status(
            response=response,
            message="Could not fetch Azure Marketplace transport operations for gardenlinux offer",
        )
        operations = response.json()
        for operation in operations:
            if AzmpTransportDest(operation["slot"]) == transport_dest and AzmpOperationState(operation["submissionState"]) == AzmpOperationState.RUNNING:
                return operation["id"]
        logger.warning("Did not find an ongoing transport operation to ship Garden Linux offer on the Azure Marketplace.")
        return "undefined"

    def fetch_operation_state(self, publisher_id: str, offer_id: str, operation_id: str):
        '''Fetches the state of a given Azure Marketplace transport operation.'''

        response = self._request(url=self._api_url(publisher_id, "offers", offer_id, "operations", operation_id))
        self._raise_for_status(
            response=response,
            message=f"Can't fetch state for transport operation {operation_id}",
        )
        operation = response.json()
        return AzmpOperationState(operation['status'])

    def go_live(self, publisher_id: str, offer_id: str):
        '''Trigger a go live operation to transport an Azure Marketplace offer to production.'''

        response = self._request(
            method='POST',
            url=self._api_url(publisher_id, "offers", offer_id, "golive"),
        )
        self._raise_for_status(
            response=response,
            message="Go live of updated gardenlinux Azure Marketplace offer failed",
        )


def _find_plan_spec(offer_spec :dict, plan_id: str):
    plan_spec = {}
    for plan in offer_spec["definition"]["plans"]:
        if plan["planId"] == plan_id:
            plan_spec = plan
            break
    else:
        raise RuntimeError(f"Plan {plan_id} not found in offer {plan_spec['id']}.")
    return plan_spec

def add_image_version_to_plan(
    spec: dict,
    plan_id: str,
    image_version: str,
    image_url: str
):
    '''
    Add a new image version to a given plan and return a modified offer spec.

    The offer spec needs to be fetched upfront from the Azure Marketplace.
    The modified offer spec needs to be pushed to the Azure Marketplace.
    '''

    plan_spec = _find_plan_spec(spec, plan_id)
    plan_spec["microsoft-azure-virtualmachines.vmImages"][image_version] = {
        "osVhdUrl": image_url,
        "lunVhdDetails": []
    }
    return spec


def remove_image_version_from_plan(spec: dict, plan_id: str, image_version: str, image_url: str):
    '''
    Remove an image version from a given plan and return a modified offer spec.

    The offer spec needs to be fetched upfront from the Azure Marketplace.
    The modified offer spec needs to be pushed to the Azure Marketplace.
    '''

    plan_spec = _find_plan_spec(spec, plan_id)
    del plan_spec["microsoft-azure-virtualmachines.vmImages"][image_version]
    return spec


def generate_urn(marketplace_cfg: glci.model.AzureMarketplaceCfg, image_version: str):
    return f"{marketplace_cfg.publisher_id}:{marketplace_cfg.offer_id}:{marketplace_cfg.plan_id}:{image_version}"


def copy_image_from_s3_to_az_storage_account(
    storage_account_cfg: glci.model.AzureStorageAccountCfg,
    s3_bucket_name: str,
    s3_object_key: str,
    target_blob_name: str,
    s3_client,
):
    ''' copy object from s3 to storage account and return the generated access url including SAS token
    for the blob
    '''
    if not target_blob_name.endswith('.vhd'):
        logger.warning(
            f"Destination image name '{target_blob_name}' does not end with '.vhd'! Resulting blob will "
            "not be suitable to create a marketplace offer from it!"
        )

    store = AzureImageStore(
        storage_account_name=storage_account_cfg.storage_account_name,
        storage_account_key=storage_account_cfg.access_key,
        container_name=storage_account_cfg.container_name,
    )

    store.copy_from_s3(
        s3_client=s3_client,
        s3_bucket_name=s3_bucket_name,
        s3_object_key=s3_object_key,
        target_blob_name=target_blob_name,
    )

    return store.get_image_url(target_blob_name)


def update_and_publish_marketplace_offer(
    service_principal_cfg: glci.model.AzureServicePrincipalCfg,
    marketplace_cfg: glci.model.AzureMarketplaceCfg,
    image_version: str,
    image_url: str,
):

    marketplace_client = AzureMarketplaceClient(
        spn_tenant_id=service_principal_cfg.tenant_id,
        spn_client_id=service_principal_cfg.client_id,
        spn_client_secret=service_principal_cfg.client_secret,
    )

    publisher_id = marketplace_cfg.publisher_id
    offer_id = marketplace_cfg.offer_id
    plan_id = marketplace_cfg.plan_id

    offer_spec = marketplace_client.fetch_offer(
        publisher_id=publisher_id,
        offer_id=offer_id,
    )

    # Add new image version to plan in the offer spec.
    modified_offer_spec = add_image_version_to_plan(
        spec=offer_spec,
        plan_id=plan_id,
        image_version=image_version,
        image_url=image_url,
    )

    # Update the marketplace offer.
    marketplace_client.update_offer(
        publisher_id=publisher_id,
        offer_id=offer_id,
        spec=modified_offer_spec,
    )

    marketplace_client.publish_offer(
        publisher_id=publisher_id,
        offer_id=offer_id,
        notification_mails=marketplace_cfg.notification_emails,
    )

    publish_operation_id = marketplace_client.fetch_ongoing_operation_id(
        publisher_id=publisher_id,
        offer_id=offer_id,
        transport_dest=AzmpTransportDest.STAGING,
    )
    return publish_operation_id


def check_offer_transport_state(
    service_principal_cfg: glci.model.AzureServicePrincipalCfg,
    marketplace_cfg: glci.model.AzureMarketplaceCfg,
    release: glci.model.OnlineReleaseManifest,
) -> glci.model.OnlineReleaseManifest:
    '''Checks the state of the gardenlinux Azure Marketplace offer transport

    In case the transport to staging enviroment has been succeeded then the transport
    to production (go live) will be automatically triggered.
    '''

    transport_state = release.published_image_metadata.transport_state
    if transport_state is glci.model.AzureTransportState.RELEASED:
        return release

    marketplace_client = AzureMarketplaceClient(
        spn_tenant_id=service_principal_cfg.tenant_id,
        spn_client_id=service_principal_cfg.client_id,
        spn_client_secret=service_principal_cfg.client_secret,
    )

    publisher_id = marketplace_cfg.publisher_id
    offer_id = marketplace_cfg.offer_id

    operation_status = marketplace_client.fetch_operation_state(
        publisher_id=publisher_id,
        offer_id=offer_id,
        operation_id=release.published_image_metadata.publish_operation_id,
    )

    # Check first if the process has been failed.
    if operation_status is AzmpOperationState.FAILED:
        published_image = glci.model.AzurePublishedImage(
            transport_state=glci.model.AzureTransportState.FAILED,
            publish_operation_id=release.published_image_metadata.publish_operation_id,
            golive_operation_id='',
            urn='',
        )
        if release.published_image_metadata.transport_state is glci.model.AzureTransportState.GO_LIVE:
            published_image.golive_operation_id = release.published_image_metadata.golive_operation_id
        return dataclasses.replace(release, published_image_metadata=published_image)

    # Publish completed. Trigger go live to transport the offer changes to production.
    if (
        transport_state is glci.model.AzureTransportState.PUBLISH
        and operation_status is AzmpOperationState.SUCCEEDED
    ):
        logger.info('Publishing of gardenlinux offer to staging succeeded. Trigger go live...')
        marketplace_client.go_live(publisher_id=publisher_id, offer_id=offer_id)
        golive_operation_id = marketplace_client.fetch_ongoing_operation_id(
            publisher_id,
            offer_id,
            AzmpTransportDest.PROD,
        )
        published_image = glci.model.AzurePublishedImage(
            transport_state=glci.model.AzureTransportState.GO_LIVE,
            publish_operation_id=release.published_image_metadata.publish_operation_id,
            golive_operation_id=golive_operation_id,
            urn='',
        )
        return dataclasses.replace(release, published_image_metadata=published_image)

    # Go Live completed. Done!
    if (
        transport_state is glci.model.AzureTransportState.GO_LIVE
        and operation_status is AzmpOperationState.SUCCEEDED
    ):
        logger.info('Tranport to production of gardenlinux offer succeeded.')
        published_image = glci.model.AzurePublishedImage(
            transport_state=glci.model.AzureTransportState.RELEASED,
            publish_operation_id=release.published_image_metadata.publish_operation_id,
            golive_operation_id=release.published_image_metadata.golive_operation_id,
            urn=generate_urn(marketplace_cfg, release.version),
        )
        return dataclasses.replace(release, published_image_metadata=published_image)

    logger.info(f"Gardenlinux Azure Marketplace release op {transport_state} is still ongoing...")
    return release

def _get_target_blob_name(version: str, generation: glci.model.AzureHyperVGeneration = None):
    if generation and generation == glci.model.AzureHyperVGeneration.V2:
        return f"gardenlinux-az-{version}-gen2.vhd"
    return f"gardenlinux-az-{version}.vhd"


def _append_hyper_v_generation(s: str, generation: glci.model.AzureHyperVGeneration = None):
    if generation and generation == glci.model.AzureHyperVGeneration.V2:
        return f"{s}-gen2"
    return s


def _create_shared_image(
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
    source_id: str
) -> CommunityGalleryImageVersion:
    image_definition_name=_append_hyper_v_generation(image_name, hyper_v_generation)

    logger.info(f'Creating gallery image definition {image_definition_name=}...')
    result = cclient.gallery_images.begin_create_or_update(
        resource_group_name=resource_group_name,
        gallery_name=gallery_name,
        gallery_image_name=image_definition_name,
        gallery_image=GalleryImage(
            location=location,
            description=shared_gallery_cfg.description,
            eula=shared_gallery_cfg.eula,
            release_note_uri=shared_gallery_cfg.release_note_uri,
            os_type=OperatingSystemTypes.LINUX,
            os_state=OperatingSystemStateTypes.GENERALIZED,
            hyper_v_generation=HyperVGeneration(hyper_v_generation.value),
            identifier=GalleryImageIdentifier(
                publisher=shared_gallery_cfg.identifier_publisher,
                offer=shared_gallery_cfg.identifier_offer,
                sku=_append_hyper_v_generation(shared_gallery_cfg.identifier_sku, hyper_v_generation),
            )
        )
    )
    logger.info('...waiting for asynchronous operation to complete')
    result = result.result()

    regions = {
        l.name
        for l in sbclient.subscriptions.list_locations(subscription_id)
    }
    regions.add(shared_gallery_cfg.location) # ensure that the gallery's location is present
    # rm regions not yet supported (although they are returned by the subscription-client)
    try:
        regions.remove('australiacentral2')
    except KeyError:
        pass
    try:
        regions.remove('brazilsoutheast')
    except KeyError:
        pass
    try:
        regions.remove('brazilus')
    except KeyError:
        pass
    try:
        regions.remove('francesouth')
    except KeyError:
        pass
    try:
        regions.remove('germanynorth')
    except KeyError:
        pass
    try:
        regions.remove('jioindiacentral')
    except KeyError:
        pass
    try:
        regions.remove('norwaywest')
    except KeyError:
        pass
    try:
        regions.remove('southafricawest')
    except KeyError:
        pass
    try:
        regions.remove('switzerlandwest')
    except KeyError:
        pass
    try:
        regions.remove('uaecentral')
    except KeyError:
        pass

    logger.info(f'Creating gallery image version {image_version=}')
    result: LROPoller[GalleryImageVersion] = cclient.gallery_image_versions.begin_create_or_update(
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
            )
        )
    )
    logger.info('...waiting for asynchronous operation to complete')
    image_version: GalleryImageVersion = result.result()

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
    credential: ClientSecretCredential,
    subscription_id : str,
    shared_gallery_cfg: glci.model.AzureSharedGalleryCfg,
) -> glci.model.AzureImageGalleryPublishedImage:

    cclient = ComputeManagementClient(credential, subscription_id)
    sbclient = SubscriptionClient(credential)

    published_name = _get_target_blob_name(release.version, hyper_v_generation)

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
        source_id=result.id
    )

    unique_id = shared_img.unique_id
    logger.info(f'Image shared: {unique_id=}')

    community_gallery_published_image = glci.model.AzureImageGalleryPublishedImage(
        hyper_v_generation=hyper_v_generation.value,
        community_gallery_image_id=unique_id,
    )

    return community_gallery_published_image


def publish_to_azure_marketplace(
    image_url: str,
    sas_token: str,
    published_version: str,
    hyper_v_generation: glci.model.AzureHyperVGeneration,
    service_principal_cfg: glci.model.AzureServicePrincipalCfg,
    marketplace_cfg: glci.model.AzureMarketplaceCfg,
) -> glci.model.AzureMarketplacePublishedImage:
    # for now, we only support Hyper-V generation V1 in Marketplace
    if hyper_v_generation != glci.model.AzureHyperVGeneration.V1:
        logger.warning(f"Publishing {hyper_v_generation} images to Azure Marketplace is currently not supported.")
        return None

    # uploading to marketplace requires an SAS token
    image_url = f"{image_url}?{sas_token}"

    # Update Marketplace offer and start publishing.
    publish_operation_id = update_and_publish_marketplace_offer(
        service_principal_cfg=service_principal_cfg,
        marketplace_cfg=marketplace_cfg,
        image_version=published_version,
        image_url=image_url
    )
    logger.info(f"Azure Marketplace publish operation ID is {publish_operation_id}")

    # use anticipated URN for now
    urn=generate_urn(marketplace_cfg, published_version)
    logger.info(f'Image shared on marketplace: {urn=}')

    marketplace_published_image = glci.model.AzureMarketplacePublishedImage(
        hyper_v_generation=hyper_v_generation.value,
        publish_operation_id=publish_operation_id,
        golive_operation_id='',
        urn=urn
    )

    return marketplace_published_image


def publish_azure_image(
    s3_client,
    release: glci.model.OnlineReleaseManifest,
    service_principal_cfg: glci.model.AzureServicePrincipalCfg,
    storage_account_cfg: glci.model.AzureStorageAccountCfg,
    shared_gallery_cfg: glci.model.AzureSharedGalleryCfg,
    marketplace_cfg: glci.model.AzureMarketplaceCfg,
    publish_to_community_gallery: bool = True,
    publish_to_marketplace: bool = False,
) -> glci.model.OnlineReleaseManifest:

    credential = ClientSecretCredential(
        tenant_id=service_principal_cfg.tenant_id,
        client_id=service_principal_cfg.client_id,
        client_secret=service_principal_cfg.client_secret
    )

    # Copy image from s3 to Azure Storage Account
    azure_release_artifact = glci.util.vm_image_artefact_for_platform('azure')
    azure_release_artifact_path = release.path_by_suffix(azure_release_artifact)

    sclient = StorageManagementClient(credential, service_principal_cfg.subscription_id)

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

    target_blob_name = _get_target_blob_name(release.version)

    logger.info(f'Copying from S3 to Azure Storage Account blob: {target_blob_name=}')
    image_url, sas_token = copy_image_from_s3_to_az_storage_account(
        storage_account_cfg=storage_account_cfg,
        s3_client=s3_client,
        s3_bucket_name=azure_release_artifact_path.s3_bucket_name,
        s3_object_key=azure_release_artifact_path.s3_key,
        target_blob_name=target_blob_name,
    )
    logger.info(f'copied from S3 to Azure Storage: {image_url=}')

    # version _must_ (of course..) be strict semver for azure
    published_version = str(version_util.parse_to_semver(release.version))

    published_image = glci.model.AzurePublishedImage(
        published_marketplace_images=[],
        published_gallery_images=[],
    )

    for hyper_v_generation in glci.model.AzureHyperVGeneration:
        if publish_to_marketplace:
            logger.info(f'Publishing Azure Marketplace image for {hyper_v_generation}...')
            marketplace_published_image = publish_to_azure_marketplace(
                image_url=image_url,
                sas_token=sas_token,
                published_version=published_version,
                hyper_v_generation=hyper_v_generation,
                service_principal_cfg=service_principal_cfg,
                marketplace_cfg=marketplace_cfg,
            )
            if marketplace_published_image != None:
                published_image.published_marketplace_images.append(marketplace_published_image)

        if publish_to_community_gallery:
            logger.info(f'Publishing community gallery image for {hyper_v_generation}...')
            gallery_published_image = publish_to_azure_community_gallery(
                image_url=image_url,
                release=release,
                published_version=published_version,
                hyper_v_generation=hyper_v_generation,
                subscription_id=service_principal_cfg.subscription_id,
                credential=credential,
                shared_gallery_cfg=shared_gallery_cfg,
            )
            published_image.published_gallery_images.append(gallery_published_image)

    return dataclasses.replace(release, published_image_metadata=published_image)
