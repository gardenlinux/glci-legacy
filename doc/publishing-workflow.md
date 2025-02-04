# Publishing images to Cloud Providers

Publishing Garden Linux images to cloud platforms requires a coherent sequence of steps - with varying complexity. This document is meant to outline the steps necessary and to provide anchors to the relevant code paths in GLCI.

## The import process outlined with bullet points

### AWS

AWS requires the `.raw` build artefact (which is the raw Garden Linux disk image) to imported as an [AMI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/AMIs.html) into EC2.

The import process involves:

- uploading the `.raw` disk image to an S3 bucket (if not done already)
- making sure that the importing EC2 subscription has read access to the `.raw` image
- import the `.raw` image as an [EBS snapshot](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-snapshots.html) to a primary EC2 region
  - make sure that the EBS snapshot is not encrypted - this may require turning off the regional [EBS enryption by default](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-by-default.html) setting
- register an AMI based on the previously imported snapshot with all relevant and required metadata (and obtain the primary AMI-ID)
- copy the AMI from the primary region to all other regions (and obtain their respective AMI-IDs)
- make sure that all AMIs are publicly accessible across all regions
- share the collected AMI-IDs along with the region they are active in

The official AWS documentation for importing VM images is at <https://docs.aws.amazon.com/vm-import/latest/userguide/vmimport-image-import.html>.

### GCP

GCP requires the `-gcpimage.tar.gz` build artefact to be imported as a [Machine Image](https://cloud.google.com/compute/docs/machine-images) into GCE.

The import process involves:

- uploading the `-gcpimage.tar.gz2` artefact to a GCS bucket (this might require downloading from S3 and re-uploading to GCS)
- importing the `-gcpimage.tar.gz2` artefact from the GCS bucket as a Machine Image specifying the architecture and a set of [Guest OS Features](https://cloud.google.com/compute/docs/images/create-custom#guest-os-features) (and obtain the image ID)
- add an IAM policy to the image that allows access to every user in GCP (see also [Issue 148](https://github.com/gardenlinux/glci/issues/148))
- share the collected image ID

The official GCP documentation for importing VM images is at <https://cloud.google.com/compute/docs/images/create-custom>.

### Azure

Azure requires the `.vhd` build artefact to be imported as a [Community Gallery Image](https://learn.microsoft.com/en-us/azure/virtual-machines/share-gallery-community).

The import process involves:

- setting up a resource group (if not yet done) in which all other resources are gathered
- create a StorageAccount in that resource group (if not already present) and upload the `.vhd` artefact to it (prior downloading from S3 is not required as uploads to an Azure StorageAccount can happen from a URL)
- create an image from the `.vhd` artefact with a storage profile with an OS disk that points to the URI of the `.vhd` in the StorageAccoung (at this point, the Hyper-V generation needs to be specified already)
- create a Community Gallery in the resource group (if not already present) and enable it to be shared as a [Community Gallery](https://learn.microsoft.com/en-us/azure/virtual-machines/share-gallery-community)
- obtain the public name of the shared Community Gallery
- create a Gallery Image _Definition_ in the Community Gallery (if not already present - AVOID changing an existing Gallery Image Definition that is being used productively) which contains all relevant metadata for that image (such as Hyper-V generation, architecture, OS type and others - see [this documentation](https://learn.microsoft.com/en-us/azure/virtual-machines/shared-image-galleries?tabs=vmsource%2Cazure-cli#image-definitions) for more details)
- create a Gallery Image _Version_ in the Gallery Image _Definition_ that links to the image created before and specify in which regions that version should be available
- assemble the image ID from the public name of the Community Gallery, the Gallery Image Definition name and the Gallery Image Version
- share the assembled image ID

The official Azure documentation on Azure Compute Image Galleries is at <https://learn.microsoft.com/en-us/azure/virtual-machines/azure-compute-gallery>

### AliCloud

GCP requires the `.qcow2` build artefact to be imported as an [Image](https://www.alibabacloud.com/help/en/ecs/user-guide/image-overview?spm=a2c63.p38356.help-menu-25365.d_4_2_0.2f162a06QLmdYT) into ECS.

The import process involves:

- uploading the `.qcow2` artefact to an OSS bucket (this might require downloading from S3 (preferrably an S3 bucket in AWS' China partition)) and re-uploading to OSS
- importing the `.qcow2` artefact from the OSS bucket as an ECS Image to a primary region, specifying a device map and guest OS features (and obtain its image ID)
- copy the image from the primary region to all other regions (and obtain their image IDs)
- turn on image sharing on all previously gathered image IDs across all regions to share the images as [Communiy Images](https://www.alibabacloud.com/help/en/ecs/user-guide/overview-12?spm=a2c63.p38356.0.i1#concept-2056865)
- share the collected image IDs

The official (English) documentation for importing custom VM images is at <https://www.alibabacloud.com/help/en/ecs/user-guide/import-images/?spm=a2c63.p38356.help-menu-25365.d_4_2_4_2.18e042c8iymkKl>.

### OpenStack

The build artefact that is required for OpenStack greatly depends on the Hypervisor that is used underneath. For VMware, the `.vmdk` artefact is required, for KVM and baremetal hypervisors, the `.qcow2` build artefact is required to be imported as an [Image](https://docs.openstack.org/glance/pike/admin/manage-images.html) to OpenStack Glance.

The import process involves:

- import the image into OpenStack Glance by pointing it to the URL of the build artefact - in each an every OpenStack region - and collect the returned Image IDs
- share the Image IDs

Othe than the previously [linked](https://docs.openstack.org/glance/pike/admin/manage-images.html) documentation, there is no other official documentation on the topic.
