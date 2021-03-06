import os
import re
import time
import string
import hashlib
import logging
import boto3
from random import choice
from public_key_converter import rsa_to_pem
from botocore.exceptions import ClientError
from cfn_resource_provider import ResourceProvider
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend as crypto_default_backend

log = logging.getLogger()

request_schema = {
    "type": "object",
    "required": ["Name"],
    "properties": {
        "Name": {"type": "string", "minLength": 1, "pattern": "[a-zA-Z0-9_/]+",
                 "description": "the name of the private key in the parameters store"},
        "Description": {"type": "string", "default": "",
                        "description": "the description of the key in the parameter store"},
        "KeyAlias": {"type": "string",
                     "default": "alias/aws/ssm",
                     "description": "KMS key to use to encrypt the key"},
        "RefreshOnUpdate": {"type": "boolean", "default": False,
                            "description": "generate a new secret on update"},
        "Version": {"type": "string",  "description": "opaque string to force update"}
    }
}


class RSAKeyProvider(ResourceProvider):

    def __init__(self):
        super(RSAKeyProvider, self).__init__()
        self.request_schema = request_schema
        self.ssm = boto3.client('ssm')
        self.iam = boto3.client('iam')
        self.region = boto3.session.Session().region_name
        self.account_id = (boto3.client('sts')).get_caller_identity()['Account']

    def convert_property_types(self):
        try:
            if 'RefreshOnUpdate' in self.properties and isinstance(self.properties['RefreshOnUpdate'], (str, unicode,)):
                self.properties['RefreshOnUpdate'] = (self.properties['RefreshOnUpdate'] == 'true')
        except ValueError as e:
            log.error('failed to convert property types %s', e)

    @property
    def allow_overwrite(self):
        return self.physical_resource_id == self.arn

    @property
    def arn(self):
        return 'arn:aws:ssm:%s:%s:parameter/%s' % (self.region, self.account_id, self.get('Name'))

    def name_from_physical_resource_id(self):
        """
        returns the name from the physical_resource_id as returned by self.arn, or None
        """
        arn_regexp = re.compile(r'arn:aws:ssm:(?P<region>[^:]*):(?P<account>[^:]*):parameter/(?P<name>.*)')
        m = re.match(arn_regexp, self.physical_resource_id)
        return m.group('name') if m is not None else None

    def get_key(self):
        response = self.ssm.get_parameter(Name=self.name_from_physical_resource_id(), WithDecryption=True)
        private_key = str(response['Parameter']['Value'])

        key = crypto_serialization.load_pem_private_key(
            private_key, password=None, backend=crypto_default_backend())

        public_key = key.public_key().public_bytes(
            crypto_serialization.Encoding.OpenSSH,
            crypto_serialization.PublicFormat.OpenSSH
        )
        return (private_key, public_key)

    def create_key(self):
        key = rsa.generate_private_key(
            backend=crypto_default_backend(),
            public_exponent=65537,
            key_size=2048
        )
        private_key = key.private_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PrivateFormat.PKCS8,
            crypto_serialization.NoEncryption())

        public_key = key.public_key().public_bytes(
            crypto_serialization.Encoding.OpenSSH,
            crypto_serialization.PublicFormat.OpenSSH
        )
        return (private_key, public_key)

    def create_or_update_secret(self, overwrite=False, new_secret=True):
        try:
            if new_secret:
                private_key, public_key = self.create_key()
            else:
                private_key, public_key = self.get_key()

            kwargs = {
                'Name': self.get('Name'),
                'KeyId': self.get('KeyAlias'),
                'Type': 'SecureString',
                'Overwrite': overwrite,
                'Value': private_key
            }
            if self.get('Description') != '':
                kwargs['Description'] = self.get('Description')

            self.ssm.put_parameter(**kwargs)

            self.set_attribute('Arn', self.arn)
            self.set_attribute('PublicKey', public_key)
            self.set_attribute('PublicKeyPEM', rsa_to_pem(public_key))
            self.set_attribute('Hash', hashlib.md5(public_key).hexdigest())

            self.physical_resource_id = self.arn
        except ClientError as e:
            self.physical_resource_id = 'could-not-create'
            self.fail(str(e))

    def create(self):
        self.create_or_update_secret(overwrite=False, new_secret=True)

    def update(self):
        self.create_or_update_secret(overwrite=self.allow_overwrite, new_secret=self.get('RefreshOnUpdate'))

    def delete(self):
        name = self.physical_resource_id.split('/', 1)
        if len(name) == 2:
            try:
                response = self.ssm.delete_parameter(Name=name[1])
            except ClientError as e:
                if e.response["Error"]["Code"] != 'ParameterNotFound':
                    return self.fail(str(e))

            self.success('System Parameter with the name %s is deleted' % name)
        else:
            self.success('System Parameter with the name %s is ignored' %
                         self.physical_resource_id)

provider = RSAKeyProvider()


def handler(request, context):
    return provider.handle(request, context)
