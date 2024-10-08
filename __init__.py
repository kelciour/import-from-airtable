# -*- coding: utf-8 -*-

"""
Anki Add-on: Import from Airtable

Import data from Airtable

Copyright: (c) 2019 Nickolay <kelciour@gmail.com>
"""

import csv
import io
import itertools
import os
import re
import requests
import shutil
import time
import traceback
import unicodedata

from aqt import mw, editcurrent, addcards, editor, modelchooser
from aqt.utils import getFile, tooltip, showText
from anki.lang import _, ngettext
from anki.hooks import wrap, addHook
from anki import models
from aqt.qt import *

from .importing import Ui_Dialog

config = mw.addonManager.getConfig(__name__)

reMedia = re.compile(r"(?i)<img[^>]+src=[\"']?([^\"'>]+)[\"']?[^>]*>|\[sound:(.*?)\]")

class AirtableImporter:

    def __init__(self):
        self.init()

    def init(self):
        d = QDialog(mw)
        d.setWindowModality(Qt.WindowModal)
        frm = Ui_Dialog()
        frm.setupUi(d)

        global config
        config = mw.addonManager.getConfig(__name__)

        def selectFile():
            file = getFile(mw, _("Select File"), None, (_("Airtable CSV Export (*.csv)")), key="import")
            if not file:
                return
            file = str(file)
            frm.csvPath.setText(file)

        frm.openBtn.clicked.connect(selectFile)
        if config['api_key']:
            frm.apiKey.setText(config['api_key'])
        frm.imgHeight.setValue(config['img_height'])

        def updateTableAndView(text):
            table, view = os.path.splitext(os.path.basename(text))[0].rsplit("-", 1)
            frm.tableName.setText(table)
            frm.viewName.setText(view)

        modelChooser = modelchooser.ModelChooser(mw, frm.modelArea, label=False)
        sizePolicy = QSizePolicy(QSizePolicy.Policy(1), QSizePolicy.Policy(0))
        modelChooser.models.setSizePolicy(sizePolicy)
        modelChooser.models.setMinimumWidth(144)
        modelChooser.models.setText("<new>")

        frm.csvPath.textChanged.connect(updateTableAndView)

        ret = d.exec_()
        modelChooser.cleanup()
        if not ret:
            return

        self.total = 0
        self.apiKey = frm.apiKey.text().strip()
        self.baseKey = frm.baseKey.text().strip()
        self.csvPath = frm.csvPath.text().strip()
        self.tableName = frm.tableName.text().strip()
        self.viewName = frm.viewName.text().strip()
        self.imgHeight = frm.imgHeight.value()
        
        mw.checkpoint("Import from Airtable")
        mw.progress.start(immediate=True)

        fieldnames = self.getFieldNames(self.csvPath)
        if modelChooser.models.text() == "<new>":
            model = self.addNewNoteType(fieldnames)
            mw.col.models.setCurrent(model)
        self.modelName = mw.col.models.current()['name']

        config['api_key'] = self.apiKey
        config['img_height'] = self.imgHeight
        config['models'][self.modelName] = {}
        config['models'][self.modelName]["base_key"] = self.baseKey
        config['models'][self.modelName]["table_name"] = self.tableName
        config['models'][self.modelName]["view_name"] = self.viewName
        config['models'][self.modelName]['metadata'] = {}
        for fld in fieldnames:
            config['models'][self.modelName]['metadata'][fld] = None
        mw.addonManager.writeConfig(__name__, config)

        did = mw.col.decks.id(self.tableName)
        thread = Downloader(self.apiKey, self.baseKey, self.tableName, self.viewName)

        def onRecv(total):
            if done:
                return
            mw.progress.update(label=ngettext("%d note imported.", "%d notes imported.", total) % total)

        done = False
        thread.recv.connect(onRecv)
        thread.start()
        while not thread.isFinished():
            mw.app.processEvents()
            thread.wait(100)

        done = True
        if not thread.error:
            records = thread.data
            for r in records:
                fields = r["fields"]
                addNewFields(self.modelName, fields)
                note = mw.col.newNote(forDeck=False)
                note['id'] = r['id']
                for f in fields:
                    if f == 'Tags':
                        note.tags = getTags(fields[f])
                        continue
                    note[f] = getFieldData(self.modelName, f, fields[f])
                note.model()['did'] = did
                mw.col.addNote(note)
                self.total += 1

        mw.addonManager.writeConfig(__name__, config)
        mw.progress.finish()
        mw.reset()

        if thread.error:
            showText(thread.error)
        else:
            tooltip(ngettext("%d note imported.", "%d notes imported.", self.total) % self.total, period=1000)

    def getFieldNames(self, csvPath):
        with io.open(csvPath, "r", encoding='utf-8-sig', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            return reader.fieldnames

    def addNewNoteType(self, fields):
        model = mw.col.models.new(self.tableName)
        model['css'] = models.defaultModel['css']
        mw.col.models.addField(model, mw.col.models.newField("id"))
        for fld in fields:
            if fld != 'Tags':
                mw.col.models.addField(model, mw.col.models.newField(fld))
        t = mw.col.models.newTemplate("Card 1")
        t['qfmt'] = "{{" + fields[0] + "}}"
        t['afmt'] = "{{FrontSide}}\n\n<hr id=answer>\n\n" + "{{" + fields[:2][-1] + "}}"
        mw.col.models.addTemplate(model, t)
        mw.col.models.add(model)
        return model

class Downloader(QThread):
    
    recv = pyqtSignal('PyQt_PyObject')

    def __init__(self, apiKey, baseKey, tableName, viewName):
        QThread.__init__(self)
        self.apiKey = apiKey
        self.baseKey = baseKey
        self.tableName = tableName
        self.viewName = viewName
        self.headers = { "Authorization": "Bearer {}".format(self.apiKey) }
        self.data = []
        self.total = 0
        self.error = None

    def run(self):
        offset = self.getRecords(self.headers)
        while offset is not None:
            time.sleep(0.2) # prevent 429 status code and 30 seconds wait before subsequent requests will succeed
            offset = self.getRecords(self.headers, offset=offset)

    def getRecords(self, headers, offset=None):
        payload = {}
        payload['view'] = self.viewName
        if offset is not None:
            payload['offset'] = offset
        try:
            r = requests.get( "https://api.airtable.com/v0/{}/{}".format(self.baseKey, self.tableName), headers=headers, params=payload )
            r.raise_for_status()
            if r.status_code == 200:
                data = r.json()
                records = data["records"]
                if "offset" in data:
                    offset = data["offset"]
                else:
                    offset = None
                self.data += data["records"]
                self.total += len(records)
                self.recv.emit(self.total)
                return offset
        except requests.exceptions.HTTPError as e:
            self.error = traceback.format_exc()
            return None

def guessExtension(contentType):
    extMap = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
    }
    return extMap[contentType]

def downloadImage(url, filename, ext):
    r = requests.get(url)
    data = r.content
    if config["img_height"] != 512:
        img = QPixmap()
        img.loadFromData(data)
        img = img.scaledToHeight(config["img_height"], Qt.SmoothTransformation)
        img_buffer = QBuffer()
        img_buffer.open(QBuffer.WriteOnly)
        img_format = ext[1:].upper()
        args = [img_format]
        if img_format == "JPG":
            args += [85]
        img.save(img_buffer, *args)
        data = img_buffer.data()
    fname = mw.col.media.writeData(filename, data)
    return fname

def downloadSound(url, filename, ext):
    r = requests.get(url)
    data = r.content
    fname = mw.col.media.writeData(filename, data)
    return fname

def uploadImage(filename):
    clientId = config.get("imgur_client_id") or "a48285b049de810"
    headers = { "Authorization": "Client-ID {}".format(clientId) }
    files = {'image': open(filename , 'rb')}
    try:
        r = requests.post( "https://api.imgur.com/3/image", headers=headers, files=files )
        r.raise_for_status()
        data = r.json()
        return data["data"]["link"]
    except requests.exceptions.HTTPError as e:
        showText(traceback.format_exc())

def uploadSound(filename):
    files = {'file': open(filename, 'rb')}
    try:
        r = requests.post('https://0x0.st', files=files)
        r.raise_for_status()
        return r.text
    except requests.exceptions.HTTPError as e:
        showText(traceback.format_exc())

def addNewFields(model, fields):
    m = mw.col.models.byName(model)
    newfields = [f for f in fields if f not in mw.col.models.fieldNames(m) and f != "Tags"]
    if not newfields:
        return
    for fld in newfields:
        mw.col.models.addField(m, mw.col.models.newField(fld))
        config['models'][model]['metadata'][fld] = None
    mw.addonManager.writeConfig(__name__, config)

def getTags(data):
    tagsTxt = unicodedata.normalize("NFC", mw.col.tags.join(data))
    return mw.col.tags.canonify(mw.col.tags.split(tagsTxt))

def getFieldData(model, fld, data):
    metadata = config['models'][model]['metadata']
    if metadata[fld] == None:
        metadata[fld] = type(data).__name__
    if isinstance(data, list) and isinstance(data[0], dict):
        arr = []
        for media in reversed(data):
            if media['id'] not in config['media']:
                ext = guessExtension(media['type'])
                filename = media['filename']
                if not filename.endswith(ext):
                    filename += ext
                if media['type'].startswith("image"):
                    if config['img_size'] == "large":
                        url = media['thumbnails']['large']['url']
                    else:
                        url = media['url']
                    fname = downloadImage(url, filename, ext)
                else:
                    url = media['url']
                    fname = downloadSound(url, filename, ext)
                config['media'][media['id']] = fname
                config['attachments'][fname] = media
            else:
                fname = config['media'][media['id']]
            if media['type'].startswith("image"):
                arr.append('<img src="{}" />'.format(fname))
            else:
                arr.append('[sound:{}]'.format(fname))
        return " ".join(arr)
    elif isinstance(data, list):
        return " ".join(data)
    else:
        return str(data)
    
def prepareData(metadata, note):
    data = {}
    data["fields"] = {}
    fields = note.keys()
    for fld in fields:
        if fld != "id":
            matches = re.findall(reMedia, note[fld])
            media = list(filter(None, itertools.chain(*matches)))
            if media:
                arr = []
                for file in media:
                    if file not in config['attachments']:
                        filename, ext = os.path.splitext(file)
                        if ext in ("jpg", "jpeg", "png", "gif"):
                            url = uploadImage(file)
                        else:
                            url = uploadSound(file)
                        arr.append({"url": url, "filename": filename})
                    else:
                        arr.append(config['attachments'][file])
                data["fields"][fld] = list(arr[::-1])
            else:
                if metadata[fld] == "list":
                    data["fields"][fld] = note[fld].split()
                elif note[fld] == "":
                    data["fields"][fld] = ""
                elif metadata[fld] == "int":
                    data["fields"][fld] = int(note[fld])
                elif metadata[fld] == "bool":
                    data["fields"][fld] = bool(note[fld])
                else:
                    data["fields"][fld] = note[fld]
    if "Tags" in metadata:
        data["fields"]["Tags"] = note.tags
    data["typecast"] = True
    return data

def updateRecord(note):
    model = note.model()['name']
    metadata = config['models'][model]["metadata"]
    data = prepareData(metadata, note)
    conf = config['models'][model]
    headers = { "Authorization": "Bearer {}".format(config['api_key']) }
    try:
        r = requests.patch("https://api.airtable.com/v0/{}/{}/{}".format(conf["base_key"], conf["table_name"], note["id"]), headers=headers, json=data )
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        showText(traceback.format_exc())

def updateNote(note):
    if not note:
        return
    model = note.model()['name']
    if model not in config['models']:
        return
    if not note["id"]:
        return
    return True

def mySaveAndClose(self, _old):
    note = self.mw.reviewer.card.note()
    if updateNote(note):
        updateRecord(note)
    ret = _old(self)
    return ret

editcurrent.EditCurrent._saveAndClose = wrap(editcurrent.EditCurrent._saveAndClose, mySaveAndClose, "around")

def saveNow(self, callback, keepFocus=False):
    note = self.note
    if updateNote(note) and self.edited:
        updateRecord(note)

editor.Editor.saveNow = wrap(editor.Editor.saveNow, saveNow, "after")

def saveTags(self):
    if not self.note:
        return
    tagsTxt = unicodedata.normalize("NFC", self.tags.text())
    if self.note.tags != mw.col.tags.canonify(mw.col.tags.split(tagsTxt)):
        self.edited = True

editor.Editor.saveTags = wrap(editor.Editor.saveTags, saveTags, "before")

def onBridgeCmd(self, cmd):
    if cmd.startswith("blur") or cmd.startswith("key"):
        self.edited = True

editor.Editor.onBridgeCmd = wrap(editor.Editor.onBridgeCmd, onBridgeCmd, "before")

def addRecord(self, note):
    model = note.model()['name']
    metadata = config['models'][model]["metadata"]
    data = prepareData(metadata, note)
    headers = { "Authorization": "Bearer {}".format(config['api_key']) }
    conf = config['models'][model]
    try:
        r = requests.post("https://api.airtable.com/v0/{}/{}".format(conf["base_key"], conf["table_name"]), headers=headers, json=data )
        r.raise_for_status()
        data = r.json()
        note["id"] = data["id"]
        note.flush()
    except requests.exceptions.HTTPError as e:
        showText(traceback.format_exc())

def myAddNote(self, note, _old):
    model = note.model()['name']
    if model not in config['models']:
        return _old(self, note)
    note["id"] = "###"
    ret = _old(self, note)
    if not ret:
        return
    addRecord(self, note)
    return note

addcards.AddCards.addNote = wrap(addcards.AddCards.addNote, myAddNote, "around")

def setupWeb(self):
    self.web.eval("""
        var style = $('<style>.airtable-id tr:nth-child(-n+2) { display: none; }</style>');
        $('html > head').append(style);
    """)

def loadNote(self, focusTo=None):
    model = self.note.model()['name']
    if model in config['models']:
        self.web.eval("$('#fields').addClass('airtable-id');")
        self.edited = False
    else:
        self.web.eval("$('#fields').removeClass('airtable-id').filter('[class=""]').removeAttr('class');")

editor.Editor.setupWeb = wrap(editor.Editor.setupWeb, setupWeb, "after")
editor.Editor.loadNote = wrap(editor.Editor.loadNote, loadNote, "before")

def removeModel(self, m):
    model = m['name']
    if model in config['models']:
        del config['models'][model]
        mw.addonManager.writeConfig(__name__, config)

models.ModelManager.rem = wrap(models.ModelManager.rem, removeModel, "after")

class AirtableUpdater:

    def __init__(self, did):
        self.did = did
        self.added = 0
        self.updated = 0
        self.init()

    def init(self):
        deck = mw.col.decks.get(self.did)['name']
        mw.checkpoint("Import from Airtable")
        mw.progress.start(immediate=True)
        done = False
        def onRecv(total):
            if done:
                return
            mw.progress.update(label=ngettext("%d note imported.", "%d notes imported.", total) % total)
        for model in config['models']:
            conf = config['models'][model]
            if conf["table_name"] == deck:
                thread = Downloader(config['api_key'], conf["base_key"], conf["table_name"], conf["view_name"])
                thread.recv.connect(onRecv)
                thread.start()
                while not thread.isFinished():
                    mw.app.processEvents()
                    thread.wait(100)
                self.importRecords(model, thread.data)
        done = True
        msg = ngettext("%d note updated.", "%d notes updated.", self.updated) % self.updated
        msg += "<br>"
        msg += ngettext("%d note added.", "%d notes added.", self.added) % self.added
        tooltip(msg, period=1500)
        mw.addonManager.writeConfig(__name__, config)
        mw.progress.finish()
        mw.reset()

    def importRecords(self, model, records):
        m = mw.col.models.byName(model)
        if not m:
            return
        mw.col.models.setCurrent(m)

        rids = {}
        nids = mw.col.models.nids(m)
        for nid in nids:
            note = mw.col.getNote(nid)
            rids[note['id']] = nid

        for r in records:
            fields = r["fields"]
            addNewFields(model, fields)
            fieldnames = mw.col.models.fieldNames(m)
            if r['id'] in rids:
                nid = rids[r['id']]
                note = mw.col.getNote(nid)
                flag = False
                for f in fieldnames:
                    if f == 'id':
                        continue
                    if f in fields:
                        val = getFieldData(model, f, fields[f])
                    else:
                        val = ""
                    if note[f] != val:
                        note[f] = val
                        flag = True
                if 'Tags' in fields:
                    val = getTags(fields['Tags'])
                    if note.tags != val:
                        note.tags = val
                        flag = True
                if flag:
                    note.flush()
                    self.updated += 1
            else:
                note = mw.col.newNote(forDeck=False)
                note['id'] = r['id']
                for f in fields:
                    if f == 'Tags':
                        note.tags = getTags(fields[f])
                        continue
                    note[f] = getFieldData(model, f, fields[f])
                note.model()['did'] = self.did
                mw.col.addNote(note)
                self.added += 1

def updateDeck(did):
    AirtableUpdater(did)

def onShowDeckOptions(m, did):
    a = m.addAction("Import from Airtable")
    a.triggered.connect(lambda b, did=did: updateDeck(did))

addHook("showDeckOptions", onShowDeckOptions)

def onImport():
    AirtableImporter()

action = QAction("Import from Airtable", mw)
action.triggered.connect(onImport)
mw.form.menuTools.addAction(action)
