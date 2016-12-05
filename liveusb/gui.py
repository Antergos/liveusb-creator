# -*- coding: utf-8 -*-
#
# Copyright © 2008-2015  Red Hat, Inc. All rights reserved.
# Copyright © 2008-2015  Luke Macken <lmacken@redhat.com>
# Copyright © 2008  Kushal Das <kushal@fedoraproject.org>
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program; if
# not, write to the Free Software Foundation, Inc., 51 Franklin Street, Fifth
# Floor, Boston, MA 02110-1301, USA. Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.
#
# Author(s): Luke Macken <lmacken@redhat.com>
#            Kushal Das <kushal@fedoraproject.org>
#            Martin Bříza <mbriza@redhat.com>

"""
A cross-platform graphical interface for the LiveUSBCreator

There was a move from the procedural code that was directly changing the UI itself.
Now, we expose a set of properties representing each object we're manipulating in the back-end.
The exposed properties are then handled independently in the UI.

For example, when the image is being written, we just change the "writing" property. The UI then locks itself up based
on this property. Basically, this means every relevant "enabled" property of the UI elements is bound to the "writing"
property of the backend.
"""

import os
import sys
import logging
import urllib.parse as urlparse
import ruamel.yaml as yaml


from time import sleep
from datetime import datetime

from PyQt5.QtCore import QVariant
from PyQt5.QtCore import pyqtProperty, pyqtSlot, QObject, QUrl, QDateTime, pyqtSignal, QThread, QAbstractListModel, QSortFilterProxyModel, QModelIndex, Qt, QTranslator, QLocale, QTimer
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QApplication
from PyQt5.QtQml import qmlRegisterType, qmlRegisterUncreatableType, QQmlComponent, QQmlApplicationEngine, QQmlListProperty, QQmlEngine
from PyQt5 import QtQuick
from setuptools.sandbox import save_pkg_resources_state

from . import resources_rc
from . import qml_rc
from . import grabber

from liveusb import LiveUSBCreator, LiveUSBError, _

try:
    import dbus.mainloop.pyqt5
    dbus.mainloop.pyqt5.DBusQtMainLoop(set_as_default=True)
except Exception as e:
    pass

config_file = open('/etc/liveusb-creator.yml', 'r').read()

CONFIG = yaml.safe_load(config_file)
MAX_FAT16 = 2047
MAX_FAT32 = 3999
MAX_EXT = 2097152

if 'Fedora' == CONFIG['DISTRO']:
    from liveusb.releases.fedora import releases, get_flavors
elif 'Antergos' == CONFIG['DISTRO']:
    from liveusb.releases.antergos import releases, get_flavors


def __(text):
    return _(text.format_map(CONFIG))


class ReleaseDownloadThread(QThread):
    """ Heavy lifting in the process the iso file download """
    downloadFinished = pyqtSignal(str)
    downloadError = pyqtSignal(str)

    beingCancelled = False

    def __init__(self, progress, proxies, filename):
        QThread.__init__(self)
        self.progress = progress
        self.proxies = proxies
        self.filename = filename

    def run(self):
        try:
            self.beingCancelled = False
            filename = grabber.download(self, self.progress.release.url, update_maximum=self.progress.start, update_current=self.progress.update)
            if filename:
                self.progress.end()
                self.downloadFinished.emit(filename)
        except LiveUSBError as e:
            self.downloadError.emit(e.args[0])

    @pyqtSlot()
    def cancelDownload(self):
        self.beingCancelled = True


class ReleaseDownload(QObject):
    """ Wrapper for the iso download process.
    It exports properties to track the percentage and the file with the result.
    """
    runningChanged = pyqtSignal()
    currentChanged = pyqtSignal()
    maximumChanged = pyqtSignal()
    pathChanged = pyqtSignal()

    _running = False
    _current = -1.0
    _maximum = -1.0
    _path = ''

    def __init__(self, parent, filename):
        QObject.__init__(self, parent)
        self.release = parent
        self._grabber = ReleaseDownloadThread(self, parent.live.get_proxies(), filename)
        self._live = parent.live

    def reset(self):
        self._running = False
        self._current = -1.0
        self._maximum = -1.0
        self.path = ''
        self.runningChanged.emit()
        self.currentChanged.emit()
        self.maximumChanged.emit()

    def start(self, size=None):
        self._maximum = size
        self._running = True
        self.maximumChanged.emit()
        self.runningChanged.emit()

    def update(self, amount_read):
        """ Update our download progressbar.

        :read: the number of bytes read so far
        """
        if self._current < amount_read:
            self._current = amount_read
            self.currentChanged.emit()

    def end(self):
        self._current = self._maximum
        self.currentChanged.emit()
        self._running = False
        self.runningChanged.emit()

    @pyqtSlot(str)
    def childFinished(self, iso):
        self.reset()
        self.path = iso
        self._running = False
        self.runningChanged.emit()

    @pyqtSlot(str)
    def childError(self, err):
        self.reset()
        self.release.addError(err)

    @pyqtSlot(str)
    def run(self):
        if len(self.parent().path) <= 0:
            self._grabber.downloadFinished.connect(self.childFinished)
            self._grabber.downloadError.connect(self.childError)
            self._grabber.start()

    @pyqtSlot()
    def cancel(self):
        self._grabber.cancelDownload()
        self.reset()

    @pyqtProperty(float, notify=maximumChanged)
    def maxProgress(self):
        return self._maximum

    @pyqtProperty(float, notify=currentChanged)
    def progress(self):
        return self._current

    @pyqtProperty(bool, notify=runningChanged)
    def running(self):
        return self._running

    @pyqtProperty(str, notify=pathChanged)
    def path(self):
        return self._path

    @path.setter
    def path(self, value):
        if self._path != value:
            self._path = value
            self._live.set_iso(value)
            self.pathChanged.emit()


class ReleaseWriterThread(QThread):
    """ The actual write to the portable drive """

    def __init__(self, parent):
        QThread.__init__(self, parent)

        self.live = parent.live
        self.parent = parent

    def run(self):
        now = datetime.now()
        try:
            self.ddImage(now)

        except Exception as e:
            self.parent.release.addError(e.args[0])
            self.live.log.exception(e)

        self.parent.running = False

    def ddImage(self, now):
        # TODO move this to the backend
        self.live.dd_image(self.update_progress)
        self.parent.status = 'Finished!'
        self.parent.finished = True
        return

    def update_progress(self, value):
        self.parent.progress = value

class ReleaseWriter(QObject):
    """ Here we can track the progress of the writing and control it """
    runningChanged = pyqtSignal()
    currentChanged = pyqtSignal()
    statusChanged = pyqtSignal()
    finishedChanged = pyqtSignal()

    _running = False
    _current = -1.0
    _status = ''
    _finished = False

    def __init__(self, parent):
        QObject.__init__(self, parent)
        self.live = parent.live
        self.release = parent
        self.worker = ReleaseWriterThread(self)

    def reset(self):
        self._running = False
        self._current = -1.0
        self.runningChanged.emit()
        self.currentChanged.emit()

    @pyqtSlot()
    def run(self):
        self._running = True
        self._current = 0.0
        self.runningChanged.emit()
        self.currentChanged.emit()
        self.status = 'Writing'
        self.worker.start()

    @pyqtSlot()
    def cancel(self):
        self.worker.terminate()
        self.reset()

    @pyqtProperty(bool, notify=runningChanged)
    def running(self):
        return self._running

    @running.setter
    def running(self, value):
        if self._running != value:
            self._running = value
            self.runningChanged.emit()

    @pyqtProperty(float, notify=currentChanged)
    def progress(self):
        return float(self._current)

    @progress.setter
    def progress(self, value):
        if (value != self._current):
            self._current = value
            self.currentChanged.emit()

    @pyqtProperty(str, notify=statusChanged)
    def status(self):
        return self._status

    @status.setter
    def status(self, s):
        if self._status != s:
            self._status = s
            self.statusChanged.emit()

    @pyqtProperty(bool, notify=finishedChanged)
    def finished(self):
        return self._finished

    @finished.setter
    def finished(self, value):
        if self._finished != value:
            self._finished = value
            self.finishedChanged.emit()


class Release(QObject):
    ''' Contains the information about the particular release of Fedora
        I think there should be a cleanup of all the properties - there seem to be more of them than needed
    '''
    screenshotsChanged = pyqtSignal()
    errorChanged = pyqtSignal()
    warningChanged = pyqtSignal()
    infoChanged = pyqtSignal()
    statusChanged = pyqtSignal()
    pathChanged = pyqtSignal()
    sizeChanged = pyqtSignal()

    _path = ''

    _archMap = {'Intel 64bit': ['x86_64'], 'Intel 32bit': ['i686','i386']} #, 'ARM': ['armv7hl']}

    def __init__(self, parent, index, live, data):
        QObject.__init__(self, parent)

        self._index = index
        self.live = live
        self.liveUSBData = parent

        self._size = 0

        self._data = data

        self._info = []
        self._warning = []
        self._error = []

        self.addWarning(_('You are about to perform a destructive install. This will erase all data and partitions on your USB drive'))

        self._download = ReleaseDownload(self, self.get_filename())
        self._download.pathChanged.connect(self.pathChanged)

        self._writer = ReleaseWriter(self)

        self.pathChanged.connect(self.statusChanged)
        self._download.runningChanged.connect(self.statusChanged)
        self._writer.runningChanged.connect(self.statusChanged)
        self._writer.statusChanged.connect(self.statusChanged)
        self.errorChanged.connect(self.statusChanged)

        parent.releaseProxyModel.archChanged.connect(self.sizeChanged)
        parent.releaseProxyModel.archChanged.connect(self.pathChanged)


    @pyqtSlot()
    def get(self):
        if len(self._path) <= 0:
            self._download.run()

    @pyqtSlot()
    def write(self):
        self._info = []
        self._warning = []
        self._error = []
        self.infoChanged.emit()
        self.errorChanged.emit()
        self.warningChanged.emit()

        self.addInfo(__('After you have tried or installed {DISTRO}, you can use {DISTRO} LiveUSB Creator to restore your flash drive to its factory settings.'))

        self._writer.run()

    @pyqtProperty(int, constant=True)
    def index(self):
        return self._index

    @pyqtProperty(bool, constant=True)
    def isSeparator(self):
        return self._data['source'] == ''

    @pyqtProperty(str, constant=True)
    def name(self):
        return self._data['name']

    def get_filename(self):
        if '.iso' in self.get_url():
            return os.path.basename(self.get_url())
        try:
            return self._data['variants']['x86_64']['filename']
        except KeyError:
            pass

        return ''

    @pyqtProperty(str, constant=True)
    def logo(self):
        return self._data['logo']

    @pyqtProperty(float, notify=sizeChanged)
    def size(self):
        if not self.isLocal:
            for arch in self._data['variants']:
                if arch in self._archMap[self.liveUSBData.releaseProxyModel.archFilter]:
                    return self._data['variants'][arch]['size']
        return self._size

    @size.setter
    def size(self, value):
        if self.isLocal and self._size != value:
            self._size = value
            self.sizeChanged.emit()

    @pyqtProperty('QStringList', constant=True)
    def arch(self):
        ret = list()
        for key in self._archMap.keys():
            for variant in self._archMap[key]:
                if variant in self._data['variants'].keys():
                    ret.append(str(key))
        return ret

    @pyqtProperty(str, constant=True)
    def version(self):
        return self._data['version']

    @pyqtProperty(QDateTime, constant=True)
    def releaseDate(self):
        return QDateTime.fromString(self._data['releaseDate'], Qt.ISODate)

    @pyqtProperty(str, constant=True)
    def summary(self):
        return self._data['summary']

    @pyqtProperty(str, constant=True)
    def description(self):
        return self._data['description']

    @pyqtProperty(str, constant=True)
    def category(self):
        if self._data['source'] in CONFIG['CATEGORIES']['main']:
            return 'main'
        elif self._data['source'] == 'Spins':
            return _('<b>Fedora Spins </b> &nbsp; Alternative desktops for Fedora')
        elif self._data['source'] == 'Labs':
            return _('<b>Fedora Labs </b> &nbsp; Functional bundles for Fedora')
        else:
            return '<b>Other</b>'

    @pyqtProperty(bool, constant=True)
    def isLocal(self):
        return self._data['source'] == 'Local'

    @pyqtProperty('QStringList', notify=screenshotsChanged)
    def screenshots(self):
        return self._data['screenshots']

    @pyqtProperty(str, constant=True)
    def url(self):
        return self.get_url()

    def get_url(self):
        if self.isLocal:
            return ''
        for arch in self._data['variants'].keys():
            if arch in self._archMap[self.liveUSBData.releaseProxyModel.archFilter]:
                return self._data['variants'][arch]['url']

    @pyqtProperty(str, notify=pathChanged)
    def path(self):
        return self._download.path

    @path.setter
    def path(self, value):
        if sys.platform.startswith('win') and value.startswith('file:///'):
            value = value.replace('file:///', '', 1)
        elif not sys.platform.startswith('win') and value.startswith('file://'):
            value = value.replace('file://', '', 1)
        if self._path != value:
            self._download.path = value
            self.pathChanged.emit()
            self.size = self.live.isosize

    @pyqtProperty(bool, notify=pathChanged)
    def readyToWrite(self):
        return len(self.path) != 0

    @pyqtProperty(ReleaseDownload, constant=True)
    def download(self):
        return self._download

    @pyqtProperty(ReleaseWriter, constant=True)
    def writer(self):
        return self._writer

    @pyqtProperty(str, notify=statusChanged)
    def status(self):
        if not self.writer.finished and not self._download.running and not self.readyToWrite and not self._writer.running and len(self._error) == 0:
            return _('Starting')
        elif self._download.running:
            return _('Downloading')
        elif len(self._error) > 0:
            return _('Error')
        elif self.readyToWrite and not self._writer.running and not self._writer.finished:
            return _('Ready to write')
        elif self._writer.status:
            return self._writer.status
        else:
            return _('Finished')

    @pyqtProperty('QStringList', notify=infoChanged)
    def info(self):
        return self._info

    def addInfo(self, value):
        if value not in self._info:
            self._info.append(value)
            self.infoChanged.emit()

    @pyqtProperty('QStringList', notify=warningChanged)
    def warning(self):
        return self._warning

    def addWarning(self, value):
        if value not in self._warning:
            self._warning.append(value)
            self.warningChanged.emit()

    @pyqtProperty('QStringList', notify=errorChanged)
    def error(self):
        return self._error

    def addError(self, value):
        if value not in self._error:
            self._error.append(str(value))
            self.errorChanged.emit()


class ReleaseListModel(QAbstractListModel):
    """ An abstraction over the list of releases to have them nicely exposed to QML and ready to be filtered
    """
    def __init__(self, parent):
        QAbstractListModel.__init__(self, parent)

    def rowCount(self, parent=QModelIndex(), *args, **kwargs):
        return len(self.parent().releaseData)

    def roleNames(self):
        return {Qt.UserRole + 1 : b'release'}

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid():
            return self.parent().releaseData[index.row()]
        return None


class ReleaseListProxy(QSortFilterProxyModel):
    """ Filtering proxy for the release list
    """
    archChanged = pyqtSignal()
    nameFilterChanged = pyqtSignal()
    isFrontChanged = pyqtSignal()

    _archFilter = ['x86_64']
    _nameFilter = ''
    _frontPage = True

    _archMap = {'Intel 64bit': ['x86_64'], 'Intel 32bit': ['i686','i386']} #, 'ARM': ['armv7hl']}
    _archMapDetailed = {'Intel 64bit': _('ISO format image for Intel, AMD and other compatible PCs (64-bit)'), 'Intel 32bit': _('ISO format image for Intel, AMD and other compatible PCs (32-bit)')} #, 'ARM': ['armv7hl']}

    def __init__(self, parent, sourceModel):
        QSortFilterProxyModel.__init__(self, parent)
        self.setSourceModel(sourceModel)

    @pyqtSlot(int, result=Release)
    def get(self, i):
        if i < 0 or i >= len(self.parent().releaseData):
            return None
        if not self.parent().releaseData[i].category:
            return None
        return self.parent().releaseData[i]

    def rowCount(self, parent=QModelIndex(), *args, **kwargs):
        if self._frontPage and self.sourceModel().rowCount(parent) > 3:
            return 3
        return self.sourceModel().rowCount(parent)

    def filterAcceptsRow(self, sourceRow, sourceParent):
        row = self.sourceModel().index(sourceRow, 0, sourceParent).data()
        if len(self._archFilter) == 0 or row.isLocal or self.archFilter in row.arch or row.isSeparator:
            if not len(self._nameFilter) or not row.isSeparator:
                if len(self._nameFilter) == 0 or self._nameFilter.lower() in row.name.lower() or self._nameFilter.lower() in row.summary.lower():
                    return True
        return False

    @pyqtProperty(str, notify=nameFilterChanged)
    def nameFilter(self):
        return self._nameFilter

    @nameFilter.setter
    def nameFilter(self, value):
        if value != self._nameFilter:
            self._nameFilter = value
            self.nameFilterChanged.emit()
            self.invalidateFilter()

    @pyqtProperty('QStringList', constant=True)
    def possibleArchs(self):
        return self._archMap.keys()

    @pyqtProperty(str, notify=archChanged)
    def archFilterDetailed(self):
        return self._archMapDetailed[self.archFilter]

    @pyqtProperty(str, notify=archChanged)
    def archFilter(self):
        for name, abbrs in self._archMap.items():
            if abbrs == self._archFilter:
                return name
        self._archFilter = self._archMap['Intel 64bit']
        return 'Intel 64bit'

    @archFilter.setter
    def archFilter(self, value):
        if value in self._archMap and self.archFilter != self._archMap[value]:
            self._archFilter = self._archMap[value]
            self.archChanged.emit()
            self.invalidateFilter()

    @pyqtProperty(bool, notify=isFrontChanged)
    def isFront(self):
        return self._frontPage

    @isFront.setter
    def isFront(self, value):
        if value != self._frontPage:
            self._frontPage = value
            self.isFrontChanged.emit()
            self.invalidate()


class LiveUSBLogHandler(logging.Handler):

    def __init__(self, cb):
        logging.Handler.__init__(self)
        self.cb = cb

    def emit(self, record):
        if record.levelname in ('INFO', 'ERROR', 'WARN'):
            self.cb(record.msg)


class USBDrive(QObject):

    beingRestoredChanged = pyqtSignal()

    def __init__(self, parent, name, drive):
        QObject.__init__(self, parent)
        self.live = parent.live
        self._name = name
        self._drive = drive
        self._beingRestored = False

    @pyqtProperty(str, constant=True)
    def text(self):
        return self._name

    @pyqtProperty(str, constant=True)
    def drive(self):
        return self._drive

    @pyqtProperty(bool, notify=beingRestoredChanged)
    def beingRestored(self):
        return self._beingRestored

    def restoreCallback(self, ok, message=None):
        self._beingRestored = False
        self.beingRestoredChanged.emit()

    @pyqtSlot()
    def restore(self):
        self._beingRestored = True
        self.beingRestoredChanged.emit()
        self.live.restore_drive(self.drive, self.restoreCallback)


class DataUpdateThread(QThread):
    def __init__(self, data):
        QThread.__init__(self)
        self.data = data

    def run(self):
        get_flavors()


class LiveUSBData(QObject):
    """ An entry point to all the exposed properties.
        There is a list of images and USB drives
    """
    releasesChanged = pyqtSignal()
    currentImageChanged = pyqtSignal()
    usbDrivesChanged = pyqtSignal()
    currentDriveChanged = pyqtSignal()
    driveToRestoreChanged = pyqtSignal()
    updateThreadStopped = pyqtSignal()
    configChanged = pyqtSignal()

    # has to be a property because you can't pass python signal parameters to qml
    _driveToRestore = None
    _config = CONFIG

    _currentIndex = 0
    _currentDrive = 0

    releaseData = []

    def __init__(self, opts):
        QObject.__init__(self)
        self.live = LiveUSBCreator(opts=opts)
        self._releaseModel = ReleaseListModel(self)
        self._releaseProxy = ReleaseListProxy(self, self._releaseModel)

        self.fillReleases()
        self.updateThread = DataUpdateThread(self)

        self._usbDrives = []

        self.live.detect_removable_drives(callback=self.USBDeviceCallback)

        self.updateThread.finished.connect(self.fillReleases)
        self.updateThread.finished.connect(self.updateThreadStopped)
        if datetime.today().year >= 2016 and datetime.today().month > 9:
            QTimer.singleShot(0, self.updateThread.start)

    @pyqtSlot()
    def fillReleases(self):
        if self.releaseData and self.releaseData[self.currentIndex]:
            if self.releaseData[self.currentIndex].download.running or self.releaseData[self.currentIndex].writer.running:
                return

        self.releaseModel.beginResetModel()

        for release in releases:
            self.releaseData.append(Release(self,
                                            len(self.releaseData),
                                            self.live,
                                            release
                                            ))

        self.releaseModel.endResetModel()
        self.releaseProxyModel.invalidate()
        self.currentImageChanged.emit()

    def USBDeviceCallback(self):
        tmpDrives = []
        previouslySelected = ''
        if len(self._usbDrives) > 0:
            previouslySelected = self._usbDrives[self._currentDrive].drive.device

        if self._driveToRestore and self._driveToRestore.beingRestored:
            return

        for drive, info in self.live.drives.items():
            name = info.friendlyName

            gb = 1000.0 # if it's decided to use base 2 values, change this

            usedSize = info.size

            if usedSize < gb ** 1:
                name += ' (%.1f B)'  % (usedSize / (gb ** 0))
            elif usedSize < gb ** 2:
                name += ' (%.1f KB)' % (usedSize / (gb ** 1))
            elif usedSize < gb ** 3:
                name += ' (%.1f MB)' % (usedSize / (gb ** 2))
            elif usedSize < gb ** 4:
                name += ' (%.1f GB)' % (usedSize / (gb ** 3))
            else:
                name += ' (%.1f TB)' % (usedSize / (gb ** 4))

            tmpDrives.append(USBDrive(self, name, info))

        if tmpDrives != self._usbDrives:
            self._usbDrives = tmpDrives
            self.usbDrivesChanged.emit()

            self._driveToRestore = None

            self.currentDrive = -1
            for i, drive in enumerate(self._usbDrives):
                if drive.drive.device == previouslySelected:
                    self.currentDrive = i
                if drive.drive.isIso9660:
                    self._driveToRestore = drive
                if self.currentDrive == -1 and not self.live.drive:
                    self.live.drive = drive.drive

            self.currentDriveChanged.emit()
            self.driveToRestoreChanged.emit()

    @pyqtProperty(QVariant, notify=configChanged)
    def config(self):
        return QVariant(self._config)

    @pyqtProperty(ReleaseListModel, notify=releasesChanged)
    def releaseModel(self):
        return self._releaseModel

    @pyqtProperty(ReleaseListProxy, notify=releasesChanged)
    def releaseProxyModel(self):
        return self._releaseProxy

    @pyqtProperty(int, notify=currentImageChanged)
    def currentIndex(self):
        return self._currentIndex

    @currentIndex.setter
    def currentIndex(self, value):
        if value != self._currentIndex:
            self._currentIndex = value
            self.currentImageChanged.emit()

    @pyqtProperty(Release, notify=currentImageChanged)
    def currentImage(self):
        if self._currentIndex < len(self.releaseData):
            return self.releaseData[self._currentIndex]
        return None

    @pyqtProperty(USBDrive, notify=driveToRestoreChanged)
    def driveToRestore(self):
        return self._driveToRestore

    @pyqtProperty(QQmlListProperty, notify=usbDrivesChanged)
    def usbDrives(self):
        return QQmlListProperty(USBDrive, self, self._usbDrives)

    @pyqtProperty('QStringList', notify=usbDrivesChanged)
    def usbDriveNames(self):
        return list(i.text for i in self._usbDrives)

    @pyqtProperty(int, notify=currentDriveChanged)
    def currentDrive(self):
        return self._currentDrive

    @currentDrive.setter
    def currentDrive(self, value):
        if len(self._usbDrives) == 0:
            self.live.drive = None
            self._currentDrive = -1
            self.currentDriveChanged.emit()
            return
        elif value > len(self._usbDrives):
            value = 0
        if self._currentDrive != value:# or not self.live.drive or self.live.drives[self.live.drive]['device'] != self._usbDrives[value].drive['device']:
            self._currentDrive = value
            if len(self._usbDrives) > 0:
                self.live.drive = self._usbDrives[self._currentDrive].drive.device
            self.currentDriveChanged.emit()
            for r in self.releaseData:
                r.download.finished = False


class LiveUSBApp(QApplication):
    """ Main application class """
    def __init__(self, opts, args):
        QApplication.__init__(self, args)
        translator = QTranslator()
        translator.load(QLocale.system().name(), "po")
        self.installTranslator(translator)
        qmlRegisterUncreatableType(ReleaseDownload, 'LiveUSB', 1, 0, 'Download', 'Not creatable directly, use the liveUSBData instance instead')
        qmlRegisterUncreatableType(ReleaseWriter, 'LiveUSB', 1, 0, 'Writer', 'Not creatable directly, use the liveUSBData instance instead')
        qmlRegisterUncreatableType(ReleaseListModel, 'LiveUSB', 1, 0, 'ReleaseModel', 'Not creatable directly, use the liveUSBData instance instead')
        qmlRegisterUncreatableType(Release, 'LiveUSB', 1, 0, 'Release', 'Not creatable directly, use the liveUSBData instance instead')
        qmlRegisterUncreatableType(USBDrive, 'LiveUSB', 1, 0, 'Drive', 'Not creatable directly, use the liveUSBData instance instead')
        qmlRegisterUncreatableType(LiveUSBData, 'LiveUSB', 1, 0, 'Data', 'Use the liveUSBData root instance')

        # releases = get_flavors()

        engine = QQmlApplicationEngine()
        self.data = LiveUSBData(opts)
        engine.rootContext().setContextProperty('liveUSBData', self.data)
        if (opts.directqml):
            engine.load(QUrl('liveusb/liveusb.qml'))
        else:
            engine.load(QUrl('qrc:/liveusb.qml'))
        engine.rootObjects()[0].show()

        self.exec_()
