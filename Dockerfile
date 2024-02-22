FROM europe-docker.pkg.dev/gardener-project/releases/cicd/job-image:1.2286.0

RUN pip3 install --upgrade \
  'azure-common==1.1.28' \
  'azure-core==1.30.0' \
  'azure-identity==1.15.0' \
  'azure-mgmt-compute==30.5.0' \
  'azure-mgmt-core==1.4.0' \
  'azure-mgmt-network~=25.3.0' \
  'azure-mgmt-resource~=23.0.1' \
  'azure-mgmt-storage~=21.1.0' \
  'azure-mgmt-subscription~=3.1.0' \
  'azure-storage-blob<13' \
  'msrestazure~=0.6.4' \
  'openstacksdk<1' \
  'oss2<3' \
  'paramiko>=2.10.1'
