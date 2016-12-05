# -*- coding: utf-8 -*-

import re
import traceback

from pyquery import pyquery

from liveusb import grabber
import ruamel.yaml as yaml
from liveusb import _, LiveUSBError
from PyQt5.QtCore import QDateTime

config_file = open('/etc/liveusb-creator.yml', 'r').read()

CONFIG = yaml.safe_load(config_file)
BASE_URL = CONFIG['BASE_URL']
ARCHES = CONFIG['ARCHES']


def getArch():
    return 'x86_64'


def _get_dl_container(d, minimal):
    tab = d('.et_pb_tab_1')
    return tab.find('.one_half').eq(0) if not minimal else tab.find('.et_column_last')


def _get_blurb_container(d, minimal):
    return d('.et_pb_blurb_0') if not minimal else d('.et_pb_blurb_1')


def getSHA(d, minimal=False):
    dl_container = _get_dl_container(d, minimal)
    return dl_container.find('li:nth-child(3)').text()


def getSize(text):
    match = re.search(r'([0-9.]+)[ ]?([KMG])B?', text)
    if not match or len(match.groups()) not in [2, 3]:
        return 0
    size = float(match.group(1))
    if match.group(2) == 'G':
        size *= 1024 * 1024 * 1024
    if match.group(2) == 'M':
        size *= 1024 * 1024
    if match.group(2) == 'K':
        size *= 1024
    return int(size)


def getDownload(d, minimal=False):
    dl_container = _get_dl_container(d, minimal)

    ret = dict()
    url = dl_container.find('h3').next().attr('href')
    version = dl_container.find('h3').next().attr('title')
    ret[getArch()] = dict(
        url=url,
        sha256=getSHA(d, minimal).replace('MD5 Sum: ', ''),
        size=getSize(dl_container.find('li:nth-child(2)').text().strip()),
        version=version.replace('Version ', ''),
        filename=dl_container.find('li:first-child').text().strip()
    )
    return ret


def getProductDetails(d, minimal=False):
    dl_container = _get_dl_container(d, minimal)
    blurb_container = _get_blurb_container(d, minimal)
    product = {
        'name': '',
        'summary': '',
        'description': '',
        'version': '',
        'releaseDate': '',
        'logo': 'qrc:/logo_antergos',
        'screenshots': [],
        'source': '',
        'variants': {'': dict(
            url='',
            sha256='',
            size=0
        )}
    }
    name = dl_container.find('h3').text().replace(' ISO', '')

    product['name'] = name
    product['source'] = name

    if minimal:
        product['summary'] = _('Installer Only')
    else:
        product['summary'] = _('Fully functional Live Antergos Environment')

    desc = ['<ul>']
    p_contents = blurb_container.find('p').text().split('^')
    ___ = [desc.append('<li>{0}</li>'.format(x.strip())) for x in p_contents if x]
    desc.append('</ul>')
    product['description'] = ''.join(desc)

    product['logo'] = 'qrc:/logo_antergos'

    download = getDownload(d, minimal)
    product['variants'] = download
    product['releaseDate'] = product['version'] = download['x86_64']['version']

    return product


def getProducts(url=BASE_URL):
    try:
        d = pyquery.PyQuery(grabber.urlread(url))
    except LiveUSBError as e:
        return []

    return [
        getProductDetails(d, minimal=False),
        getProductDetails(d, minimal=True)
    ]


def get_flavors(store=True):
    r = []
    products = getProducts()

    if products:
        r += products
    r += [{'name': _('Custom OS...'),
                  'description': _('<p>Here you can choose a OS image from your hard drive to be written to your flash disk</p><p>Currently it is only supported to write raw disk images (.iso or .bin)</p>'),
                  'logo': 'qrc:/icon_folder',
                  'screenshots': [],
                  'summary': _('Pick a file from your drive(s)'),
                  'version': '',
                  'releaseDate': '',
                  'source': 'Local',
                  'variants': {'': dict(url='', sha256='', size=0)}}]

    if store and len(r) > 1:
        releases[:] = r

    print(r)
    return r


releases = []

if __name__ == '__main__':
    import pprint
    #pprint.pprint(get_fedora_flavors(False))
