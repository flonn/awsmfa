#!/usr/bin/python3
# coding=utf-8
import argparse
import configparser
import datetime
import getpass
import sys

import boto3.session
import botocore
import botocore.exceptions
import botocore.session
import os
from ._version import VERSION

SIX_HOURS_IN_SECONDS = 21600
OK = 0
USER_RECOVERABLE_ERROR = 1


def main(args=None):
    args = parse_args(args)

    if args.version:
        print(VERSION)
        return OK

    credentials = configparser.ConfigParser(default_section=None)
    credentials.read(args.aws_credentials)

    session = botocore.session.Session(profile=args.identity_profile)
    try:
        session3 = boto3.session.Session(botocore_session=session)
    except botocore.exceptions.ProfileNotFound as err:
        print(str(err), file=sys.stderr)
        print("Available profiles: %s" %
              ", ".join(sorted(session.available_profiles)))
        return USER_RECOVERABLE_ERROR

    serial_number = find_mfa_for_user(args.serial_number, session, session3)
    if not serial_number:
        print("There are no MFA devices associated with this user.",
              file=sys.stderr)
        return USER_RECOVERABLE_ERROR

    if args.token_code is None:
        while args.token_code is None or len(args.token_code) != 6:
            args.token_code = getpass.getpass("MFA Token Code: ")

    sts = session3.client('sts')
    try:
        if args.role_to_assume:
            response = sts.assume_role(
                DurationSeconds=min(args.duration, 3600),
                RoleArn=args.role_to_assume,
                RoleSessionName=args.role_session_name,
                SerialNumber=serial_number,
                TokenCode=args.token_code)
        else:
            response = sts.get_session_token(
                DurationSeconds=args.duration,
                SerialNumber=serial_number,
                TokenCode=args.token_code)
    except botocore.exceptions.ClientError as err:
        if err.response["Error"]["Code"] == "AccessDenied":
            print(str(err), file=sys.stderr)
            return USER_RECOVERABLE_ERROR
        else:
            raise
    remaining = response['Credentials']['Expiration'] - datetime.datetime.now(
        tz=datetime.timezone.utc)
    print("Temporary credentials will expire in %s." % remaining)
    update_credentials_file(args, credentials, response['Credentials'])
    return OK


def parse_args(args):
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog='awsmfa',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--version',
                        default=False,
                        action='store_true',
                        help='Display version number and exit.')
    parser.add_argument('role_to_assume',
                        nargs='?',
                        metavar='role-to-assume',
                        default=os.environ.get('AWS_MFA_ROLE_TO_ASSUME'),
                        help='Full ARN of the role you wish to assume. If not '
                             'provided, the temporary credentials will '
                             'inherit the user\'s policies. The temporary '
                             'credentials will also satisfy the '
                             'aws:MultiFactorAuthPresent condition variable. '
                             'If the AWS_MFA_ROLE_TO_ASSUME environment '
                             'variable is set, it will be used as the default '
                             'value.')
    parser.add_argument('--aws-credentials',
                        default=os.path.join(os.path.expanduser('~'),
                                             '.aws/credentials'),
                        help='Full path to the ~/.aws/credentials file.')
    parser.add_argument('-d', '--duration',
                        type=int,
                        default=int(os.environ.get('AWS_MFA_DURATION',
                                                   SIX_HOURS_IN_SECONDS)),
                        help='The number of seconds that you wish the '
                             'temporary credentials to be valid for. For role '
                             'assumption, this will be limited to an hour. If '
                             'the AWS_MFA_DURATION environment variable is '
                             'set, it will be used as the default value.')
    parser.add_argument('-i', '--identity-profile',
                        default=os.environ.get('AWS_MFA_IDENTITY_PROFILE',
                                               'identity'),
                        help='Name of the section in the credentials file '
                             'representing your long-lived credentials. '
                             'All values in this section '
                             '(including custom parameters such as "region" '
                             'or "s3") will be copied to the '
                             '--target-profile, with the access key, secret '
                             'key, and session key replaced by the temporary '
                             'credentials. If the AWS_MFA_IDENTITY_PROFILE '
                             'environment variable is set, it will be used as '
                             'the default value.')
    parser.add_argument('--serial-number',
                        default=os.environ.get('AWS_MFA_SERIAL_NUMBER', None),
                        help='Full ARN of the MFA device. If not provided, '
                             'this will be read from the '
                             'AWS_MFA_SERIAL_NUMBER environment variable or '
                             'queried from IAM automatically. For automatic '
                             'detection to work, your identity profile must '
                             'have IAM policies that allow "aws iam '
                             'get-user" and "aws iam list-mfa-devices".')
    parser.add_argument('-t', '--target-profile',
                        default=os.environ.get('AWS_MFA_TARGET_PROFILE',
                                               'default'),
                        help='Name of the section in the credentials file to '
                             'overwrite with temporary credentials. This '
                             'defaults to "default" because most tools read '
                             'that profile. The existing values in this '
                             'profile will be overwritten. If the '
                             'AWS_MFA_TARGET_PROFILE environment variable is '
                             'set, it will be used as the default value.')
    parser.add_argument('--role-session-name',
                        default='awsmfa_%s' % datetime.datetime.now().strftime(
                            '%Y%m%dT%H%M%S'),
                        help='The name of the temporary session. Applies only '
                             'when assuming a role.')
    parser.add_argument('-c', '--token-code',
                        default=os.environ.get('AWS_MFA_TOKEN_CODE'),
                        help='The 6 digit numeric MFA code generated by your '
                             'device. If the AWS_MFA_TOKEN_CODE environment '
                             'variable is set, it will be used as the default '
                             'value.')
    args = parser.parse_args(args)
    return args


def find_mfa_for_user(user_specified_serial, botocore_session, boto3_session):
    if user_specified_serial:
        return user_specified_serial

    iam = boto3_session.client('iam')
    user = iam.get_user()
    if user['User']['Arn'].endswith(':root'):
        # The root user MFA device is not in the same way as non-root
        # users, so we must find the root MFA devices using a different
        # method than we do for normal users.
        devices = boto3_session.resource('iam').CurrentUser().mfa_devices.all()
        serials = (x.serial_number for x in devices)
    else:
        # Non-root users can have a restrictive policy that allows them
        # only to list devices associated with their user but it requires
        # using the low level IAM client to compose the proper request.
        username = user['User']['UserName']
        devices = botocore_session.create_client('iam').list_mfa_devices(
            UserName=username)
        serials = (x['SerialNumber'] for x in devices['MFADevices'])

    serials = list(serials)
    if not serials:
        return None
    if len(serials) > 1:
        print("Warning: user has %d MFA devices. Using the first." %
              len(devices), file=sys.stderr)
    return serials[0]


def update_credentials_file(args, credentials, temporary_credentials):
    credentials.remove_section(args.target_profile)
    credentials.add_section(args.target_profile)
    for k, v in credentials.items(args.identity_profile):
        credentials.set(args.target_profile, k, v)
    credentials.set(args.target_profile, 'aws_access_key_id',
                    temporary_credentials['AccessKeyId'])
    credentials.set(args.target_profile, 'aws_secret_access_key',
                    temporary_credentials['SecretAccessKey'])
    credentials.set(args.target_profile, 'aws_session_token',
                    temporary_credentials['SessionToken'])
    credentials.set(args.target_profile, 'awsmfa_expiration',
                    temporary_credentials['Expiration'].isoformat())
    temp_credentials_file = args.aws_credentials + ".tmp"
    with open(temp_credentials_file, "w") as out:
        credentials.write(out)
    os.rename(temp_credentials_file, args.aws_credentials)


if __name__ == '__main__':
    status = main()
    sys.exit(status)
