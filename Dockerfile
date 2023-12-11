FROM europe-docker.pkg.dev/gardener-project/releases/cicd/job-image:1.2286.0

RUN pip3 install --upgrade \
  'azure-common==1.1.28' \
  'azure-core==1.26.0' \
  'azure-identity==1.6.0' \
  'azure-mgmt-compute==28.0.1' \
  'azure-mgmt-core==1.3.2' \
  'azure-mgmt-network~=19.2.0' \
  'azure-mgmt-resource~=20.0.0' \
  'azure-mgmt-storage~=18.0.0' \
  'azure-mgmt-subscription~=3.1.0' \
  'azure-storage-blob<13' \
  'msrestazure~=0.6.4' \
  'openstacksdk<1' \
  'oss2<3' \
  'paramiko>=2.10.1'
