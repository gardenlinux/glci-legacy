import base64
import dataclasses
import functools
import io
import tempfile
import time
import logging
import os

import ctx
import google.cloud.storage.blob
import google.cloud.storage.client
import google.oauth2.service_account
import googleapiclient.errors
import googleapiclient.discovery

import glci.model
import glci.util


logger = lambda: logging.getLogger(__name__)


def upload_image_to_gcs_bucket(
    storage_client: google.cloud.storage.Client,
    s3_client,
    release: glci.model.OnlineReleaseManifest,
    gcp_publishing_cfg: glci.model.PublishingTargetGCP,
) -> google.cloud.storage.blob.Blob:

    gcp_release_artifact = glci.util.vm_image_artefact_for_platform('gcp')
    gcp_release_artifact_path = release.path_by_suffix(gcp_release_artifact)
    raw_image_key = gcp_release_artifact_path.s3_key

    image_blob_name = f'gardenlinux-{release.version}.tar.gz'
    s3_bucket_name = release.s3_bucket
    gcp_bucket_name = gcp_publishing_cfg.gcp_bucket_name

    # XXX: rather do streaming
    with tempfile.TemporaryFile() as tfh:
        resp = s3_client.get_object(
            Bucket=s3_bucket_name,
            Key=raw_image_key,
        )
        size = resp['ContentLength']
        logger().info(f'downloading image from {s3_bucket_name=} to temporary location ({size=})')

        s3_client.download_fileobj(
            Bucket=s3_bucket_name,
            Key=raw_image_key,
            Fileobj=tfh,
        )
        logger().info(f'downloaded image from {s3_bucket_name=}')

        # get the size of the temp file on local disk
        tfh.seek(0, os.SEEK_END)
        size = tfh.tell()
        tfh.seek(0)

        logger().info(f'uploading image from temporary location to gcp {gcp_bucket_name=} {image_blob_name=} ({size=})')
        gcp_bucket = storage_client.get_bucket(gcp_bucket_name)
        image_blob = gcp_bucket.blob(image_blob_name)
        image_blob.upload_from_file(
            tfh,
            content_type='application/x-xz',
            size=size,
            timeout=600, # allow for a longer upload timeout on slow connections
        )
        logger().info(f'uploaded image {raw_image_key=} to {image_blob_name=}')
        return image_blob


def delete_image_from_gcs_bucket(
    storage_client: google.cloud.storage.Client,
    release: glci.model.OnlineReleaseManifest,
    gcp_publishing_cfg: glci.model.PublishingTargetGCP,
    dry_run: bool
):
    gcp_bucket_name = gcp_publishing_cfg.gcp_bucket_name
    image_blob_name = f'gardenlinux-{release.version}.tar.gz'

    if dry_run:
        logger().warning(f"DRY RUN: would delete {image_blob_name=} in {gcp_bucket_name=}")
        return

    gcp_bucket = storage_client.get_bucket(gcp_bucket_name)
    image_blob = gcp_bucket.blob(image_blob_name)
    if image_blob.exists():
        logger().info(f"deleting {image_blob_name=} in {gcp_bucket_name=}")
        image_blob.delete()


def insert_image_to_gce_image_store(
    compute_client,
    s3_client,
    image_blob: google.cloud.storage.blob.Blob,
    gcp_project_name: str,
    release: glci.model.OnlineReleaseManifest,
) -> glci.model.OnlineReleaseManifest:
    image_name = _get_image_name_from_release_manifest(release, hashed=True)

    images = compute_client.images()

    body = {
        'description': 'gardenlinux',
        'name': image_name,
        'rawDisk': {
            'source': image_blob.generate_signed_url(int(time.time())),
        },
        'guestOsFeatures': [
            {
                'type': 'VIRTIO_SCSI_MULTIQUEUE',
            },
            {
                'type': 'UEFI_COMPATIBLE',
            },
            {
                'type': 'GVNIC',
            },
        ],
        'architecture': _get_gcp_compliant_architecture_identifier(release.architecture),
    }

    if release.secureboot:
        logger().info('retrieving secureboot certificates')

        buf = io.BytesIO()
        s3_client.download_fileobj(
            Bucket=release.s3_bucket,
            Key=release.path_by_suffix('.secureboot.pk.der').s3_key,
            Fileobj=buf,
        )
        pk = base64.b64encode(buf.getvalue()).decode()

        buf = io.BytesIO()
        s3_client.download_fileobj(
            Bucket=release.s3_bucket,
            Key=release.path_by_suffix('.secureboot.kek.der').s3_key,
            Fileobj=buf,
        )
        keks = base64.b64encode(buf.getvalue()).decode()

        buf = io.BytesIO()
        s3_client.download_fileobj(
            Bucket=release.s3_bucket,
            Key=release.path_by_suffix('.secureboot.db.der').s3_key,
            Fileobj=buf,
        )
        dbs = base64.b64encode(buf.getvalue()).decode()

        body['shieldedInstanceInitialState'] = {
            'pk': {
                'content': pk,
                'fileType': 'X509'
            },
            'keks': [
                {
                    'content': keks,
                    'fileType': 'X509'
                }
            ],
            'dbs': [
                {
                    'content': dbs,
                    'fileType': 'X509'
                }
            ]
        }

    insertion_rq = images.insert(
        project=gcp_project_name,
        body=body
    )

    logger().info(f'inserting new image {image_name=} into project {gcp_project_name=}')

    resp = insertion_rq.execute()
    op_name = resp['name']

    logger().info(f'waiting for {op_name=}')

    operation = compute_client.globalOperations()

    # this can take more than two minutes, so we allow up to 20 minutes
    max_retries = 10
    logger().info("waiting up to 20 minutes for image insert operation to complete")

    for retry in range(max_retries):
        try:
            operation.wait(
                project=gcp_project_name,
                operation=op_name,
            ).execute()
            break
        except TimeoutError as e:
            if retry + 1 >= max_retries:
                raise e
            else:
                pass

    logger().info(f'import done - removing temporary object from bucket {image_blob.name=}')

    image_blob.delete()

    # make image public
    iam_policies = images.getIamPolicy(
        project=gcp_project_name, resource=image_name
    ).execute()
    if not 'bindings' in iam_policies:
        iam_policies = []
    iam_policies.append({
        'members': ['allAuthenticatedUsers'],
        'role': 'roles/compute.imageUser',
    })

    images.setIamPolicy(
        project=gcp_project_name,
        resource=image_name,
        body={
            'bindings': iam_policies,
        }
    ).execute()

    published_image = glci.model.GcpPublishedImage(
        gcp_image_name=image_name,
        gcp_project_name=gcp_project_name,
    )

    return dataclasses.replace(release, published_image_metadata=published_image)


def delete_image_from_gce_image_store(
    compute_client,
    gcp_project_name: str,
    release: glci.model.OnlineReleaseManifest,
    dry_run: bool
):
    image_name = _get_image_name_from_release_manifest(release, hashed=True)

    images = compute_client.images()

    if dry_run:
        logger().warning(f"DRY RUN: would delete {image_name=} in {gcp_project_name=}")
        return

    logger().info(f'deleting stale image {image_name=} from project {gcp_project_name=}')

    deletion_rq = images.delete(
        project=gcp_project_name,
        image=image_name,
    )

    resp = deletion_rq.execute()
    op_name = resp['name']

    logger().info(f'waiting for {op_name=}')

    operation = compute_client.globalOperations()
    operation.wait(
        project=gcp_project_name,
        operation=op_name,
    ).execute()

    logger().info(f'image {image_name=} deleted')


def upload_and_publish_image(
    storage_client: google.cloud.storage.Client,
    s3_client,
    compute_client,
    gcp_project_name: str,
    release: glci.model.OnlineReleaseManifest,
    gcp_publishing_cfg: glci.model.PublishingTargetGCP,
):
    image_blob = upload_image_to_gcs_bucket(
        storage_client=storage_client,
        s3_client=s3_client,
        release=release,
        gcp_publishing_cfg=gcp_publishing_cfg,
    )

    try:
        release_manifest = insert_image_to_gce_image_store(
            compute_client=compute_client,
            s3_client=s3_client,
            image_blob=image_blob,
            gcp_project_name=gcp_project_name,
            release=release,
        )
    except googleapiclient.errors.HttpError as e:
        if e.status_code == 409:
            # image already exists, delete it first and retry
            delete_image_from_gce_image_store(
                compute_client=compute_client,
                gcp_project_name=gcp_project_name,
                release=release,
                dry_run=False
            )
            release_manifest = insert_image_to_gce_image_store(
                compute_client=compute_client,
                s3_client=s3_client,
                image_blob=image_blob,
                gcp_project_name=gcp_project_name,
                release=release,
            )
        else:
            raise

    return release_manifest


def cleanup_image(
    storage_client: google.cloud.storage.Client,
    compute_client,
    gcp_project_name: str,
    release: glci.model.OnlineReleaseManifest,
    gcp_publishing_cfg: glci.model.PublishingTargetGCP,
    dry_run: bool
):
    delete_image_from_gce_image_store(
        compute_client=compute_client,
        gcp_project_name=gcp_project_name,
        release=release,
        dry_run=dry_run
    )

    delete_image_from_gcs_bucket(
        storage_client=storage_client,
        release=release,
        gcp_publishing_cfg=gcp_publishing_cfg,
        dry_run=dry_run
    )


def _get_image_name_from_release_manifest(release: glci.model.OnlineReleaseManifest, hashed) -> str:
    return f'gardenlinux-{release.canonical_release_manifest_key_suffix(hashed=hashed)}'.replace(
        '.', '-'
    ).replace(
        '_', '-'
    ).strip('-')

def _get_gcp_compliant_architecture_identifier(arch: glci.model.Architecture):
    """
    Get proper string per architecture as documented here:
        https://cloud.google.com/compute/docs/reference/rest/v1/images/insert
        > The architecture of the image. Valid values are ARM64 or X86_64.
    """
    if arch == glci.model.Architecture.AMD64:
        return 'X86_64'
    if arch == glci.model.Architecture.ARM64:
        return 'ARM64'
    raise Exception(f"Invalid architecture {arch}")


def _to_gcp_cfg(gcp_cfg: str):
    if isinstance(gcp_cfg, str):
        cfg_factory = ctx.cfg_factory()
        gcp_cfg = cfg_factory.gcp(gcp_cfg)
    return gcp_cfg


def credentials(gcp_cfg: str):
    gcp_cfg = _to_gcp_cfg(gcp_cfg=gcp_cfg)

    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        gcp_cfg.service_account_key(),
    )

    return creds


def authenticated_build_func(gcp_cfg: str):
    creds = credentials(gcp_cfg=gcp_cfg)

    return functools.partial(googleapiclient.discovery.build, credentials=creds)


def cloud_storage_client(gcp_cfg: str, *args, **kwargs):
    gcp_cfg = _to_gcp_cfg(gcp_cfg=gcp_cfg)
    creds = credentials(gcp_cfg=gcp_cfg)

    return google.cloud.storage.Client(
        project=gcp_cfg.project(),
        credentials=creds,
        *args,
        **kwargs,
    )
