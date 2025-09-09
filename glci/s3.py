import os
import tempfile

import glci.aws
import glci.model
import glci.util


def s3_client_for_aws_cfg_name(aws_cfg_name: str):
    return glci.aws.session(aws_cfg_name).client('s3')


def s3_resource_for_aws_cfg_name(aws_cfg_name: str):
    return glci.aws.session(aws_cfg_name).resource('s3')


def upload_dir(
    s3_resource,
    bucket_name: str,
    src_dir_path: str,
    dest_dir_path: str = "/",
):
    bucket = s3_resource.Bucket(name=bucket_name)
    for dirpath, _, filenames in os.walk(src_dir_path):
        for filename in filenames:
            src_file_path = os.path.join(dirpath, filename)

            # clean-up filename
            processed_filename = filename.replace('+', '')

            relative_file_path = os.path.relpath(
                os.path.join(dirpath, processed_filename),
                start=src_dir_path,
            )

            dst_file_path = os.path.join(dest_dir_path, relative_file_path)

            if os.path.exists(src_file_path) and os.path.isfile(src_file_path):
                bucket.upload_file(
                    Filename=src_file_path,
                    Key=dst_file_path,
                )


def download_file(
    s3_resource,
    bucket_name: str,
    s3_key: str,
    local_dir: str,
    filename: str = 'file'
) -> str:
    bucket = s3_resource.Bucket(name=bucket_name)
    local_dir = os.path.abspath(os.path.realpath(local_dir))
    path_to_file = os.path.join(local_dir, filename)
    bucket.download_file(
        Key=s3_key,
        Filename=path_to_file,
    )
    return path_to_file


def upload_file(
    s3_resource,
    bucket_name: str,
    s3_key: str,
    file_path: str,
):
    bucket = s3_resource.Bucket(name=bucket_name)

    bucket.upload_file(
        Filename=file_path,
        Key=s3_key,
    )


def download_dir(
    s3_resource,
    bucket_name: str,
    s3_dir: str,
    local_dir: str,
):
    bucket = s3_resource.Bucket(name=bucket_name)

    local_dir = os.path.abspath(os.path.realpath(local_dir))

    for s3_obj in bucket.objects.filter(Prefix=s3_dir):

        s3_dirname, s3_filename = os.path.split(s3_obj.key)
        local_dest_dir = os.path.join(local_dir, s3_dirname)
        local_dest_file_path = os.path.join(local_dest_dir, s3_filename)

        os.makedirs(local_dest_dir, exist_ok=True)

        bucket.download_file(Key=s3_obj.key, Filename=local_dest_file_path)


def _transport_release_artifact(
    release_manifest: glci.model.OnlineReleaseManifest,
    source_cfg_name: str,
    destination_cfg_name: str,
    platform: glci.model.Platform = 'aws',
):
    # Copy the relevant release-artefact from the source-bucket to the destination-bucket.
    # It is assumed there _is_ a bucket present in the destination with the same name as in the
    # source partition
    with tempfile.TemporaryDirectory() as tmp_dir:
        session = glci.aws.session(aws_cfg=source_cfg_name)
        resource = session.resource('s3')
        s3_release_file = release_manifest.path_by_suffix(
            glci.util.vm_image_artefact_for_platform(platform)
        )
        artefact_file_path = download_file(
            s3_resource=resource,
            bucket_name=s3_release_file.s3_bucket_name,
            local_dir=tmp_dir,
            s3_key=s3_release_file.s3_key,
        )
        session = glci.aws.session(aws_cfg=destination_cfg_name)
        resource = session.resource('s3')
        upload_file(
            s3_resource=resource,
            bucket_name=s3_release_file.s3_bucket_name,
            s3_key=s3_release_file.s3_key,
            file_path=artefact_file_path,
        )