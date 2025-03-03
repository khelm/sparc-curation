import sys
import json
from urllib import parse
from pathlib import Path
from getpass import getpass
from argparse import Namespace
from oauth2client import file, client
from .config import auth


def get_auth_code(url):
    import robobrowser
    br = robobrowser.RoboBrowser()
    br.open(url)
    form = br.get_form(id=0)
    if form is None:
        raise ValueError('No form! Do you have the right client id?')
    print('If you registered using google please navigate to\n'
          'the url below and leave email and password blank.')
    print()
    print(url)
    print()
    print(form)
    print()
    print('protocols.io OAuth form')
    e = form['email'].value = input('Email: ')
    p = form['password'].value = getpass()
    if e and p:
        br.submit_form(form)
        params = dict(parse.parse_qsl(parse.urlsplit(br.url).query))

    elif (not e or not p) or 'code' not in params:
        print('If you are logging in via a 3rd party services\n'
              'please paste the redirect url in the prompt')
        manual_url = input('redirect url: ')
        params = dict(parse.parse_qsl(parse.urlsplit(manual_url).query))
        if 'code' not in params:
            print('No auth code provided. Exiting ...')
            sys.exit(10)

    code = params['code']
    return code


class MyOA2WSF(client.OAuth2WebServerFlow):
    """ monkey patch to fix protocols.io non compliance with the oauth standard """
    def step1_get_authorize_url(self):
        value = super().step1_get_authorize_url()
        return value.replace('redirect_uri', 'redirect_url')


client.OAuth2WebServerFlow = MyOA2WSF


def run_flow(flow, storage):
    url = flow.step1_get_authorize_url()
    code = get_auth_code(url)
    try:
        credential = flow.step2_exchange(code)
    except client.FlowExchangeError as e:
        sys.exit('Authentication has failed: {0}'.format(e))

    storage.put(credential)
    credential.set_store(storage)
    print('Authentication successful.')

    return credential


def get_protocols_io_auth(creds_file,
                          store_file=auth.get_path('protocols-io-api-store-file')):
    flags = Namespace(noauth_local_webserver=True,
                      logging_level='INFO')
    sfile = store_file
    store = file.Storage(sfile.as_posix())
    creds = store.get()
    SCOPES = 'readwrite'
    if not creds or creds.invalid:
        cfile = creds_file
        with open(cfile, 'rt') as f:
            redirect_uri, *_ = json.load(f)['installed']['redirect_uris']
        client.OOB_CALLBACK_URN = redirect_uri  # hack to get around google defaults
        flow = client.flow_from_clientsecrets(cfile.as_posix(),
                                              scope=SCOPES,
                                              redirect_uri=redirect_uri)

        creds = run_flow(flow, store)

    return creds
