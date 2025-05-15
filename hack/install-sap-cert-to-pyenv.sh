#!/bin/bash

# This script changes what root CA certificates your local pyenv is trusting

CA_CERT_PEM_FILE=$(python -c 'import certifi; print(certifi.where())')

# backup your certfile
cp $CA_CERT_PEM_FILE $CA_CERT_PEM_FILE.bak

# get the CA cert from SAP and append file to $CA_CERT_PEM_FILE
curl http://aia.pki.co.sap.com/aia/SAP%20Global%20Root%20CA.crt >> $CA_CERT_PEM_FILE
curl http://aia.pki.co.sap.com/aia/SAPNetCA_G2_2.crt >> $CA_CERT_PEM_FILE

