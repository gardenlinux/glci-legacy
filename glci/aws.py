import concurrent.futures
import dataclasses
import enum
import io
import logging
import pprint
import time
import typing
import functools

import boto3
import botocore.client
from botocore.exceptions import ClientError
import ctx
import model.aws

import glci.model
import glci.util


logger = logging.getLogger(__name__)


def response_ok(response: dict):
    resp_meta = response['ResponseMetadata']
    if (status_code := resp_meta['HTTPStatusCode']) == 200:
        return response

    raise RuntimeError(f'rq {resp_meta["RequestId"]=} failed {status_code=}')


class TaskStatus(enum.Enum):
    ACTIVE = 'active'
    COMPLETED = 'completed'
    DELETED = 'deleted' # indicates an error (image was rejected)


class ImageState(enum.Enum):
    PENDING = 'pending'
    AVAILABLE = 'available'
    INVALID = 'invalid'
    DEREGISTERED = 'deregistered'
    TRANSIENT = 'transient'
    FAILED = 'failed'
    ERROR = 'error'

    def is_erroneous(self):
        if self in (
            ImageState.INVALID,
            ImageState.FAILED,
            ImageState.ERROR,
        ):
            return True
        else:
            return False


def import_snapshot(
    ec2_client: botocore.client.BaseClient,
    s3_bucket_name: str,
    image_key: str,
) -> str:
    """
    @return import_task_id (use for `wait_for_snapshot_import`)
    """
    res = ec2_client.import_snapshot(
        ClientData={
            'Comment': 'uploaded by gardenlinux-cicd',
        },
        Description='uploaded by gardenlinux-cicd',
        DiskContainer={
            'UserBucket': {
                'S3Bucket': s3_bucket_name,
                'S3Key': image_key,
            },
            'Format': 'raw',
            'Description': 'uploaded by gardenlinux-cicd',
        },
        Encrypted=False,
    )

    import_task_id = res['ImportTaskId']
    return import_task_id


def wait_for_snapshot_import(
    ec2_client: botocore.client.BaseClient,
    snapshot_task_id: str,
    polling_interval_seconds: int=15,
):
    """
    @return snapshot_id
    """
    def describe_import_snapshot_task():
        st = ec2_client.describe_import_snapshot_tasks(
            ImportTaskIds=[snapshot_task_id]
        )['ImportSnapshotTasks'][0]
        return st

    def current_status() -> TaskStatus:
        st = describe_import_snapshot_task()
        st = TaskStatus(st['SnapshotTaskDetail']['Status'])
        return st

    def snapshot_id():
        st = describe_import_snapshot_task()
        task_id = st['SnapshotTaskDetail']['SnapshotId']
        return task_id

    while not (status := current_status()) is TaskStatus.COMPLETED:
        logger.info(f'{snapshot_task_id=}: {status=}')

        if status is TaskStatus.DELETED:
            status = describe_import_snapshot_task()
            details = status['SnapshotTaskDetail']
            raise RuntimeError(f'image uploaded by {snapshot_task_id=} was rejected: {details=}')
        time.sleep(polling_interval_seconds)

    return snapshot_id()


def _to_aws_architecture(
    architecture: glci.model.Architecture,
) -> str:
    """Convert the Value of our Architecture-enum to the values AWS knows/expects
    """
    match architecture:
        case glci.model.Architecture.AMD64:
            return 'x86_64'
        case glci.model.Architecture.ARM64:
            return 'arm64'
        case _:
            raise NotImplementedError(architecture)


def register_image(
    ec2_client: botocore.client.BaseClient,
    snapshot_id: str,
    image_name: str,
    architecture: str,
    uefi_required: bool,
    uefi_data: typing.Optional[str],
) -> str:
    """
    @return: ami-id of registered image
    """
    root_device_name = '/dev/xvda'

    params = {
        # 'ImageLocation': XX, s3-url?
        'Architecture': architecture,
        'BootMode': 'uefi-preferred',
        'ImdsSupport': 'v2.0',  # Ensure we're using the hardened IMDS version
        'BlockDeviceMappings': [
            {
                'DeviceName': root_device_name,
                'Ebs': {
                    'DeleteOnTermination': True,
                    'SnapshotId': snapshot_id,
                    'VolumeType': 'gp3',
                }
            }
        ],
        'Description': 'gardenlinux',
        'EnaSupport': True,
        'Name': image_name,
        'RootDeviceName': root_device_name,
        'VirtualizationType': 'hvm'  # | paravirtual
    }
    if uefi_required:
        params['BootMode'] = 'uefi'
    if uefi_data:
        params['BootMode'] = 'uefi'
        params['TpmSupport'] = 'v2.0'
        params['UefiData'] = uefi_data

    result = ec2_client.register_image(**params)

    # XXX need to wait until image is available (before publishing)
    return result['ImageId']


def enumerate_region_names(
    ec2_client: botocore.client.BaseClient,
    regions_to_include: list[str] = None
):
    for region in ec2_client.describe_regions()['Regions']:
        region_name = region['RegionName']
        if regions_to_include is not None:
            if region_name in regions_to_include:
                yield region_name
        else:
            yield region_name


def wait_for_image_state(
    ec2_client: botocore.client.BaseClient,
    image_id: str,
    target_state=ImageState.AVAILABLE,
    polling_interval_seconds: int=15,
):
    def current_image_state():
        image_details = ec2_client.describe_images(ImageIds=[image_id])['Images'][0]
        return ImageState(image_details['State'])

    while not (image_state := current_image_state()) is target_state:
        if image_state.is_erroneous():
            raise RuntimeError(f'{image_id=}: {image_state=}')
        time.sleep(polling_interval_seconds)

    return image_state


def wait_for_images(
    mk_session: callable,
    region_img_map: typing.Dict[str, str], # {region_name: ami_id}
    target_state=ImageState.AVAILABLE,
):
    logger.info(f'will wait for {len(region_img_map)} image(s) to reach {target_state=}')
    for region_name, image_id in region_img_map.items():
        logger.info(f'waiting for {image_id=}')
        # as we want to wait for _all_ images, it should be OK not to parallelise polling
        image_state = wait_for_image_state(
            ec2_client=mk_session(region_name=region_name).client('ec2'),
            image_id=image_id,
            target_state=target_state,
        )
        logger.info(f'{image_id=} reached state {image_state=}')
    logger.info('all images reached target-state')


def set_images_public(
    mk_session: callable,
    region_img_map: typing.Dict[str, str], # {region_name: ami_id}
):
    for region_name, image_id in region_img_map.items():
        session_ = mk_session(region_name=region_name)
        ec2_client = session_.client('ec2')

        try:
            res = ec2_client.modify_image_attribute(
                Attribute='launchPermission',
                ImageId=image_id,
                LaunchPermission={
                    'Add': [
                        {
                            'Group': 'all',
                        },
                    ],
                },
            )
            response_ok(res)

        except ClientError as e:
            logger.error(f"Failed to set {image_id=} public in {region_name=}")
            raise e


def copy_image(
    mk_session: callable,
    ami_image_id: str,
    image_name: str,
    src_region_name: str,
    target_regions: typing.Sequence[str],
):
    """
    @param mk_session: callable accepting `region_name`, returning authenticated boto3-session
    """
    for target_region in target_regions:
        if target_region == src_region_name:
            continue

        session_ = mk_session(region_name=target_region)
        ec2_client = session_.client('ec2')

        res = ec2_client.copy_image(
            SourceImageId=ami_image_id,
            SourceRegion=src_region_name,
            Name=image_name,
            CopyImageTags=True
        )
        # XXX: return (new) image-AMI
        response_ok(res)
        yield target_region, res['ImageId']


def image_ids_by_name(
    mk_session: callable,
    image_name: str,
    region_names: typing.Sequence[str],
):
    for region_name in region_names:
        session_ = mk_session(region_name=region_name)
        ec2 = session_.client('ec2')
        images = ec2.describe_images(Filters=[{'Name': 'name', 'Values': [image_name]}])
        # there must either be one or none
        images = images['Images']
        if len(images) < 1:
            logger.warning(f'did not find {image_name=} in {region_name=}')
            continue
        if len(images) > 1:
            raise ValueError('found more than one image (this is a bug)')

        yield region_name, images[0]['ImageId']


def unregister_images_by_name(
    mk_session: callable,
    image_name: str,
    dry_run: bool,
    region_names: typing.Sequence[str]=None
):
    if not region_names:
        ec2_client = mk_session().client('ec2')
        region_names = tuple(enumerate_region_names(ec2_client=ec2_client))
    else:
        region_names = tuple(region_names)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(region_names))

    results = []
    for region_name, image_id in image_ids_by_name(
        mk_session=mk_session,
        image_name=image_name,
        region_names=region_names,
    ):
        def unregister_image():
            ec2 = mk_session(region_name=region_name).client('ec2')
            if dry_run:
                logger.warning(f'DRY RUN: would unregister {image_id=} in {region_name}')
            else:
                ec2.deregister_image(ImageId=image_id)
                logger.info(f'unregistered {image_id=} in {region_name}')
        results.append(executor.submit(unregister_image))

    concurrent.futures.wait(results)


def _get_snapshots_for_image(
        client: botocore.client.BaseClient,
        image_id: str,
):
    snapshots = []
    response = client.describe_images(
        Owners=["self"], Filters=[{"Name": "image-id", "Values": [image_id]}]
    )

    if response["Images"]:
        image = response["Images"][0]

        for bdm in image.get("BlockDeviceMappings"):
            snapshots.append(bdm["Ebs"]["SnapshotId"])
    else:
        logger.warning(f"no snapshots found for {image_id=}")
    return snapshots


def _delete_snapshots(
        client: botocore.client.BaseClient,
        snapshot_ids: list[str],
        dry_run: bool
):
    for snapshot in snapshot_ids:
        if dry_run:
            logger.warning(f"DRY RUN: would delete {snapshot=}")
        else:
            client.delete_snapshot(SnapshotId=snapshot)
            logger.info(f"deleted {snapshot=}")


def unregister_images_by_id(
    mk_session: callable,
    images: glci.model.AwsPublishedImageSet,
    dry_run: bool
):
    executor = concurrent.futures.ThreadPoolExecutor()
    results = []

    for image in images.published_aws_images:
        def unregister_image(
            img,
        ):
            ec2 = mk_session(region_name=img.aws_region_id).client('ec2')
            response = response_ok(ec2.describe_images(
                Owners=["self"], Filters=[{"Name": "image-id", "Values": [img.ami_id]}]
            ))

            if len(response["Images"]) == 0:
                logger.warning(f"AMI with id {img.ami_id} is no longer present in {img.aws_region_id}")
            else:
                snapshots = _get_snapshots_for_image(ec2, img.ami_id)
                if dry_run:
                    logger.warning(f'DRY RUN: would unregister {img.ami_id=} in {img.aws_region_id}')
                else:
                    ec2.deregister_image(ImageId=img.ami_id)
                    logger.info(f'unregistered {img.ami_id=} in {img.aws_region_id}')
                _delete_snapshots(ec2, snapshots, dry_run=dry_run)

        results.append(executor.submit(unregister_image(image)))

    concurrent.futures.wait(results)


def import_image(
    ec2_client: botocore.client.BaseClient,
    s3_bucket_name: str,
    image_key: str,
):
    """
    triggers import-image

    note: if e.g. a uefi partition is detected, aws will reject image
    workaround: use import_snapshot (where no such validation is done)
    """
    res = ec2_client.import_snapshot(
        ClientData={
            'Comment': 'uploaded by gardenlinux-cicd',
        },
        Description='uploaded by gardenlinux-cicd',
        DiskContainers=[{
            'UserBucket': {
                'S3Bucket': s3_bucket_name,
                'S3Key': image_key,
            },
            'Format': 'raw',
            'Description': 'uploaded by gardenlinux-cicd',
        }],
    )

    import_task_id = res['ImportTaskId']
    return import_task_id


def target_image_name_for_release(release: glci.model.OnlineReleaseManifest):
    target_image_name = f'gardenlinux-{release.canonical_release_manifest_key_suffix()}'
    return target_image_name


def calculate_aws_tags(
    image_tag_cfg: glci.model.ImageTagConfiguration,
    release: glci.model.OnlineReleaseManifest,
) -> list[dict[str, str]]:
    tags = []
    if not image_tag_cfg:
        return tags
    if image_tag_cfg.include_gardenlinux_version:
        tags.append({"Key": "gardenlinux-version", "Value": release.version})
    if image_tag_cfg.include_gardenlinux_committish:
        tags.append({"Key": "gardenlinux-committish", "Value": release.build_committish})
    for s in image_tag_cfg.static_tags:
        tags.append({"Key": s, "Value": image_tag_cfg.static_tags[s]})
    return tags


def attach_tags(
        ec2_client: botocore.client.BaseClient,
        resources: list[str],
        tags: list[dict[str, str]]
):
    if len(tags) == 0:
        return
    
    _ = response_ok(ec2_client.create_tags(
        Resources=resources,
        Tags=tags
    ))


def upload_and_register_gardenlinux_image(
    aws_publishing_cfg: glci.model.PublishingTargetAWS,
    publishing_cfg: glci.model.PublishingCfg,
    release: glci.model.OnlineReleaseManifest,
) -> glci.model.OnlineReleaseManifest:
    published_images = []
    for aws_cfg in aws_publishing_cfg.aws_cfgs:
        aws_cfg: glci.model.PublishingTargetAWSAccount
        aws_cfg_name = aws_cfg.aws_cfg_name
        if aws_cfg_name.endswith('-cn') and release.secureboot:
            logger.info(f'AWS China does not support secureboot, skipping')
            continue

        tags = calculate_aws_tags(aws_publishing_cfg.image_tags, release)

        logger.info(
            f'Running AWS-Publication for aws-config {aws_cfg_name}.'
        )
        _session = session(aws_cfg=aws_cfg_name)
        mk_session = functools.partial(session, aws_cfg=aws_cfg_name)
        ec2_client = _session.client('ec2')
        s3_client = _session.client('s3')

        aws_release_artifact = glci.util.vm_image_artefact_for_platform('aws')
        aws_release_artifact_path = release.path_by_suffix(aws_release_artifact)

        bucket_cfg_name = publishing_cfg.buildresult_bucket(name=aws_cfg.buildresult_bucket)
        bucket_name = bucket_cfg_name.bucket_name

        # TODO: check path is actually S3ReleaseFile
        raw_image_key = aws_release_artifact_path.s3_key

        target_image_name = target_image_name_for_release(release=release)

        try:
            snapshot_task_id = import_snapshot(
                ec2_client=ec2_client,
                s3_bucket_name=bucket_name,
                image_key=raw_image_key,
            )
        except Exception as e:
            logger.error(f'error {e} while trying to import snapshot')
            logger.error(f'hint: check whether {bucket_name=} {raw_image_key=} is public')
            raise
        logger.info(f'started import {snapshot_task_id=}')

        snapshot_id = wait_for_snapshot_import(
            ec2_client=ec2_client,
            snapshot_task_id=snapshot_task_id,
        )
        attach_tags(ec2_client=ec2_client, resources=[snapshot_id], tags=tags)
        logger.info(f'import task finished {snapshot_id=}')

        uefi_data = None
        if release.secureboot:
            logger.info('retrieving secureboot certificates')

            buf = io.BytesIO()
            s3_client.download_fileobj(
                Bucket=bucket_name,
                Key=release.path_by_suffix('.secureboot.aws-efivars').s3_key,
                Fileobj=buf,
            )
            uefi_data = buf.getvalue().decode()

        initial_ami_id = register_image(
            ec2_client=ec2_client,
            snapshot_id=snapshot_id,
            image_name=target_image_name,
            architecture=_to_aws_architecture(release.architecture),
            uefi_required=release.require_uefi,
            uefi_data=uefi_data,
        )
        attach_tags(ec2_client=ec2_client, resources=[initial_ami_id], tags=tags)
        logger.info(f'registered {initial_ami_id=}')

        region_names = tuple(enumerate_region_names(ec2_client=ec2_client, regions_to_include=aws_cfg.copy_regions))

        try:
            image_map = dict(
                copy_image(
                    mk_session=mk_session,
                    ami_image_id=initial_ami_id,
                    image_name=target_image_name,
                    src_region_name=_session.region_name,
                    target_regions=region_names,
                )
            )
        except:
            logger.warning('an error occurred whilst copying images - will remove them')
            unregister_images_by_name(
                mk_session=mk_session,
                image_name=target_image_name,
                dry_run=False,
                region_names=region_names,
            )
            raise

        # dict{<region_name>: <ami_id>}

        # add origin image
        image_map[_session.region_name] = initial_ami_id

        published_images.extend(
            glci.model.AwsPublishedImage(
                ami_id=ami_id,
                aws_region_id=region_name,
                image_name=target_image_name,
            ) for region_name, ami_id in image_map.items()
        )

        image_map_pretty = pprint.pformat(image_map)
        logger.info(f'copied images: {image_map_pretty}')

        wait_for_images(
            mk_session=mk_session,
            region_img_map=image_map,
        )
        logger.info(f'all {len(image_map)} images became "ready"')

        set_images_public(
            mk_session=mk_session,
            region_img_map=image_map,
        )
        logger.info(f'all {len(image_map)} images were set to "public"')

    published_image_set = glci.model.AwsPublishedImageSet(
        published_aws_images=tuple(published_images)
    )
    return dataclasses.replace(release, published_image_metadata=published_image_set)


def session(
    aws_cfg: str | model.aws.AwsProfile,
    region_name: str=None,
):
    if isinstance(aws_cfg, str):
        cfg_factory = ctx.cfg_factory()
        aws_cfg = cfg_factory.aws(aws_cfg)

    region_name = region_name or aws_cfg.region()

    return boto3.Session(
        aws_access_key_id=aws_cfg.access_key_id(),
        aws_secret_access_key=aws_cfg.secret_access_key(),
        region_name=region_name,
    )
