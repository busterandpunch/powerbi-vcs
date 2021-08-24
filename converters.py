import ast
import json
import os
from os.path import dirname
import re
import struct
import zipfile
from io import BytesIO
from operator import itemgetter

from lxml import etree


class Converter:

    def raw_to_vcs(self, b, *args, **kwargs):
        raise NotImplementedError("Converter.raw_to_vcs must be extended!")

    def vcs_to_raw(self, b, *args, **kwargs):
        raise NotImplementedError("Converter.vcs_to_raw must be extended!")

    def raw_to_textconv(self, b, *args, **kwargs):
        # Fall back to vcs format if no special handling
        return self.raw_to_vcs(b, *args, **kwargs).decode('utf-8')

    def write_raw_to_vcs(self, b, vcspath, *args, **kwargs):
        self.dir = os.path.dirname(vcspath)
        os.makedirs(self.dir, exist_ok=True)
        with open(vcspath, 'wb') as f:
            f.write(self.raw_to_vcs(b, *args, **kwargs))

    def write_vcs_to_raw(self, vcspath, rawzip, *args, **kwargs):
        self.dir = os.path.dirname(vcspath)
        with open(vcspath, 'rb') as f:
            rawzip.write(self.vcs_to_raw(f.read(), *args, **kwargs))

    def write_raw_to_textconv(self, b, outio, *args, **kwargs):
        print(self.raw_to_textconv(b, *args, **kwargs), file=outio)


class NoopConverter(Converter):

    def raw_to_vcs(self, b):
        return b

    def vcs_to_raw(self, b):
        return b

    def raw_to_textconv(self, b, *args, **kwargs):
        import hashlib
        my_SHA256 = hashlib.sha256()
        my_SHA256.update(b)

        return "File hash: " + my_SHA256.hexdigest() + "\n"


class XMLConverter(Converter):

    LXML_ENCODINGS = {
        'utf-8-sig': 'utf-8',
        'utf-16-le': 'utf-16'
    }

    def __init__(self, encoding, xml_declaration):
        self.encoding = encoding
        self.xml_declaration = xml_declaration
        # Note that lxml doesn't recognize the encoding names e.g. 'utf-8-sig' or 'utf-16-le' (they're recognized as
        # 'utf-8' and 'utf-16' respectively). Hence the little hack below:
        self.lxml_encoding = self.LXML_ENCODINGS.get(encoding, encoding)

    def raw_to_vcs(self, b):
        """ Convert xml from the raw pbit to onse suitable for version control - i.e. nicer encoding, pretty print, etc. """

        parser = etree.XMLParser(remove_blank_text=True)

        # If no encoding is specified in the XML, all is well - we can decode it then pass the unicode to the parser.
        # However, if encoding is specified, then lxml won't accept an already decoded string - so we have to pass it
        # the bytes (and let it decode).
        m = re.match(
            b'^.{,4}\<\?xml [^\>]*encoding=[\'"]([a-z0-9_\-]+)[\'"]', b)
        if m:
            xml_encoding = m.group(1).decode('ascii')
            if xml_encoding.lower() != self.lxml_encoding.lower():
                raise ValueError("TODO")
            root = etree.fromstring(b, parser)
        else:
            root = etree.fromstring(b.decode(self.encoding), parser)

        # return pretty-printed, with XML, in UTF-8
        return etree.tostring(root, pretty_print=True, xml_declaration=self.xml_declaration, encoding='utf-8')

    def vcs_to_raw(self, b):
        """ Convert from the csv version on xml to the raw form - i.e. not pretty printing and getting the encoding right """

        parser = etree.XMLParser(remove_blank_text=True)
        # note that vcs is always in UTF-8, which is encoded in the xml, so no need to specify
        root = etree.fromstring(b, parser)
        # We do the decode and encode at the end so that e.g. if it's meant to be 'utf-8-sig', lxml_enc will be 'utf-8'
        # (which will be encoded in the xml), but we need to add the three -sig bytes to make it 'utf-8-sig'.
        return etree.tostring(root, pretty_print=False, xml_declaration=self.xml_declaration, encoding=self.lxml_encoding).decode(self.lxml_encoding).encode(self.encoding)


class JSONConverter(Converter):

    EMBEDDED_JSON_KEY = '__powerbi-vcs-embedded-json__'
    REFERENCED_ENTRY_KEY = '__powerbi-vcs-reference__'
    MULTILINE_KEY = "__powerbi-vcs-multiline__"
    REFERENCED_VALUE = "value"
    SORT_KEYS = False  # format seems dependent on key order which is ... odd.

    def __init__(self, encoding):
        self.encoding = encoding

    def _store_multiline_strings_in_array(self, v):
        """
        Break multi line strings into an array
        Note powerBI consistently uses \n rather than \r\n

        e.g.
        {"v": "hello\nworld"}

        becomes
        {"v": {"__powerbi-vcs-multiline__":[
            "hello",
            "world"
        ]}}
        """
        if isinstance(v, str) and "\n" in v:
            return {self.MULTILINE_KEY: v.split("\n")}
        elif isinstance(v, dict):
            return {kk: self._store_multiline_strings_in_array(vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._store_multiline_strings_in_array(vv) for vv in v]
        else:
            return v

    def _rebuild_multiline_strings_from_array(self, v):
        if isinstance(v, dict):
            if len(v) == 1 and self.MULTILINE_KEY in v:
                return "\n".join(v[self.MULTILINE_KEY])
            return {kk: self._rebuild_multiline_strings_from_array(vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._rebuild_multiline_strings_from_array(vv) for vv in v]
        else:
            return v

    def _store_large_entries_as_references(self, k, v):
        """
        Some documents become unmanageable,
        break report layout out by visualContainer (tab)
        and DataModelSchema out by table into files in subdirectory alongside file
        """
        if isinstance(v, dict):
            return {kk: self._store_large_entries_as_references(kk, vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            modified = [self._store_large_entries_as_references(
                None, vv) for vv in v]
            if (k != "tables" and k != "sections" and k != "bookmarks") or len(v) == 0 or not isinstance(v[0], dict) or not "name" in v[0]:
                return modified
            return [
                {self.REFERENCED_ENTRY_KEY: self._store_reference(k, vv)} for vv in modified]
        else:
            return v

    def _store_reference(self, folder, entry):
        # Handily the lists we want to store all have a nice name property
        name = entry.get("displayName", entry.get("name"))
        safe_name = "".join(
            [c for c in name if re.match(r'\w', c)])
        filename = os.path.join(self.dir, folder, safe_name + ".json")
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        json_string = json.dumps({self.REFERENCED_VALUE: entry}, indent=2,
                                 ensure_ascii=False,  # so embedded e.g. copyright symbols don't be munged to unicode codes
                                 sort_keys=self.SORT_KEYS).encode('utf-8')

        with open(filename, 'wb') as f:
            f.write(json_string)

        relative_path = os.path.relpath(filename, self.dir)
        return relative_path

    def _dereference_references(self, v):
        if isinstance(v, dict):
            if len(v) == 1 and self.REFERENCED_ENTRY_KEY in v:
                return self._dereference_reference(v[self.REFERENCED_ENTRY_KEY])
            return {kk: self._dereference_references(vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._dereference_references(vv) for vv in v]
        else:
            return v

    def _dereference_reference(self, filename):
        with open(os.path.join(self.dir, filename), 'rb') as f:
            return json.loads(f.read()).get(self.REFERENCED_VALUE)

    def _sort_visual_containers(self, k, v):
        if isinstance(v, dict):
            return {kk: self._sort_visual_containers(kk, vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            modified = [self._sort_visual_containers(None, vv) for vv in v]
            if (k != "visualContainers"):
                return modified
            return sorted(modified, key=lambda v: v.get("z", v.get("id")))
        else:
            return v

    def _ignore_volatile_dates(self, k, v):
        """
        Certain dates are just too volatile, so set any volatile dates to epoch start
        """
        if isinstance(v, str) and k == "modifiedTime" or k == "structureModifiedTime" or k == "refreshedTime":
            return "1699-12-31T00:00:00"
        elif isinstance(v, dict):
            return {kk: self._ignore_volatile_dates(kk, vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._ignore_volatile_dates(None, vv) for vv in v]
        else:
            return v

    def _renumber_element_ids(self, k, v, containing_list_key, id_index=None):
        """
        Ids are volatile re-order ids from dicts which are in lists called visualContainers
        """
        if isinstance(v, dict):
            return {kk: self._renumber_element_ids(kk, vv, containing_list_key, id_index) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._renumber_element_ids(None, vv, k, ix) for ix, vv in enumerate(v)]
        elif containing_list_key == "visualContainers" and k == "id":
            return id_index
        else:
            return v

    def _ignore_objectids(self, k, v):
        """
        ObjectIds are volatile and completely unecessary - remove them from all dicts
        """
        if isinstance(v, dict):
            return {kk: self._ignore_objectids(kk, vv) for kk, vv in v.items() if kk != "objectId"}
        elif isinstance(v, list):
            return [self._ignore_objectids(None, vv) for vv in v]
        else:
            return v

    def _jsonify_embedded_json(self, v):
        """
        Some pbit json has embedded json strings. To aid readability and diffs etc., we make sure we load and format
        these too. To make sure we're aware of this, we follow the encoding:

        ```
        x: "{\"y\": 1 }"
        ```

        becomes

        ```
        x: { EMBEDDED_JSON_KEY: { "y": 1 } }
        ```
        """
        if isinstance(v, str):
            try:
                d = json.loads(v)
                if isinstance(d, (dict, list)):
                    return {self.EMBEDDED_JSON_KEY: d}
                else:
                    return v
            except Exception as e:
                return v
        elif isinstance(v, dict):
            return {kk: self._jsonify_embedded_json(vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._jsonify_embedded_json(vv) for vv in v]
        else:
            return v

    def _undo_jsonify_embedded_json(self, v):
        """
        Unfo jsonify_embedded_json e.g.

        ```
        x: { EMBEDDED_JSON_KEY: { "y": 1 } }
        ```

        becomes

        ```
        x: "{\"y\": 1 }"
        ```
        """
        if isinstance(v, dict):
            if len(v) == 1 and self.EMBEDDED_JSON_KEY in v:
                return json.dumps(v[self.EMBEDDED_JSON_KEY], separators=(',', ':'), ensure_ascii=False, sort_keys=self.SORT_KEYS)
            return {kk: self._undo_jsonify_embedded_json(vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            return [self._undo_jsonify_embedded_json(vv) for vv in v]
        else:
            return v

    def raw_to_vcs(self, b):
        """
        Converts raw json from pbit into that ready for vcs - modifying json to improve diff-ability
            Embedded json strings broken out into substrings
            Volatile dates set to start of epoch
            Lists of large objects (pages, tables etc) stored in path with reference
        """

        raw_json_string = b.decode(self.encoding)
        raw_json = json.loads(raw_json_string)
        cooked_json = raw_json
        cooked_json = self._jsonify_embedded_json(cooked_json)
        if self.diffable:
            cooked_json = self._ignore_volatile_dates(None, cooked_json)
            # Sorting visual containers while useful doesn't work...
            #cooked_json = self._sort_visual_containers(None, cooked_json)
            #cooked_json = self._renumber_element_ids(None, cooked_json, None)

            cooked_json = self._store_multiline_strings_in_array(cooked_json)
            cooked_json = self._store_large_entries_as_references(
                None, cooked_json)

        return json.dumps(cooked_json, indent=2,
                          # so embedded e.g. copyright symbols don't be munged to unicode codes
                          ensure_ascii=False,
                          sort_keys=self.SORT_KEYS).encode('utf-8')

    def vcs_to_raw(self, b):
        """
        Converts vcs json to that used in pbit - removing modifications to improve diff-ability
            Embedded json strings broken out into substrings
            Lists of large objects (pages, tables etc) stored in path with reference
        """
        raw_json = json.loads(b.decode('utf-8'))
        cooked_json = raw_json
        if self.diffable:
            cooked_json = self._dereference_references(cooked_json)
            cooked_json = self._rebuild_multiline_strings_from_array(
                cooked_json)
        cooked_json = self._undo_jsonify_embedded_json(cooked_json)
        return json.dumps(cooked_json, separators=(',', ':'), ensure_ascii=False, sort_keys=self.SORT_KEYS).encode(self.encoding)

    def raw_to_textconv(self, b):
        """ Converts raw json from pbit into that ready for diffing - mainly just prettification """

        return json.dumps(self._jsonify_embedded_json(json.loads(b.decode(self.encoding))), indent=2,
                          ensure_ascii=False,  # so embedded e.g. copyright symbols don't be munged to unicode codes
                          sort_keys=True) + "\n"


class DataMashupConverter(Converter):
    """
    The DataMashup file is a bit funky. The format is (roughly):
        - 4 null bytes
        - 4 bytes representing little-endian int for length of next zip
        - bytes (of length above) as zip
        - 4 bytes representing little-endian int for length of next xml
        - utf-8-sig xml of above length
        - 4 bytes representing little-endian int - which seems to be 34 more than the one two below:
        - 4 null bytes
        - 4 bytes representing little-endian int for length of next xml
        - xml of this length
        - the four bytes 16 00 00 00
        - a zip End (!) Of Central Directory record (indicated by the bytes 50 4b 05 06)
          https://en.wikipedia.org/wiki/Zip_(file_format)#End_of_central_directory_record_(EOCD)
          which is a bit surprising in this location, since there's no associated start of the zip file.
          After some experiments, Power BI will not work if everything after 16 00 00 00 is omitted,
          and also not if everything after 50 4b 05 06 is omitted, claiming the file has been corrupted.
          If the tail of the file is replaced with that of a different .pbix file, there are no noticeable
          errors in opening the modified .pbix file.
        - Some bytes further along in this file, I found the sequence 
          01 00 00 00 D0 8C 9D DF 01 15 D1 11 8C 7A 00 C0 4F C2 97 EB 01 00 00 00 to be matching across
          several different .pbix files. Even longer matches can be found across revisions of the
          same .pbix file. Maybe this is metadata about the version of Power BI that was used, and other
          metadata, since it seems harmless to transplant everything after the previously mentioned 16 00 00 00.
    """

    CONVERTERS = {
        '[Content_Types].xml': XMLConverter('utf-8-sig', True),
        'Config/Package.xml': XMLConverter('utf-8-sig', True),
        'Formulas/Section1.m': NoopConverter()
    }

    def write_raw_to_vcs(self, b, outdir):
        """ Convert the raw format into multiple separate files that are more readable """

        if b[:4] != b'\x00\x00\x00\x00':
            raise ValueError("TODO")
        len1 = int.from_bytes(b[4:8], byteorder="little")
        start1 = 8
        end1 = start1 + len1
        zip1 = b[start1:end1]
        start2 = end1 + 4
        len2 = int.from_bytes(b[end1:start2], byteorder="little")
        end2 = start2 + len2
        xml1 = b[start2:end2]
        b8 = b[end2:end2+8]
        start3 = end2 + 12
        len3 = int.from_bytes(b[end2 + 8: start3], byteorder="little")
        if int.from_bytes(b[end2:end2+4], "little") - len3 != 34:
            raise ValueError("TODO")
        end3 = start3 + len3
        xml2 = b[start3:end3]
        extra = b[end3:]

        # extract header zip:
        with zipfile.ZipFile(BytesIO(zip1)) as zd:
            order = []
            # read items (in the order they appear in the archive)
            for name in zd.namelist():
                order.append(name)
                outfile = os.path.join(outdir, name)
                # create folder if needed
                os.makedirs(os.path.dirname(outfile), exist_ok=True)
                conv = self.CONVERTERS[name]
                conv.write_raw_to_vcs(zd.read(name), outfile)

        # write order:
        open(os.path.join(outdir, ".zo"), 'w').write("\n".join(order))

        # now write the xmls and bytes between:
        # open(os.path.join(outdir, 'DataMashup', "1.int"), 'wb').write(b[4:8])
        XMLConverter('utf-8-sig', True).write_raw_to_vcs(xml1,
                                                         os.path.join(outdir, "3.xml"))
        XMLConverter('utf-8-sig', True).write_raw_to_vcs(xml2,
                                                         os.path.join(outdir, "6.xml"))
        NoopConverter().write_raw_to_vcs(extra, os.path.join(outdir, "7.bytes"))

    def write_vcs_to_raw(self, vcs_dir, rawzip):

        # zip up the header bytes:
        b = BytesIO()
        with zipfile.ZipFile(b, mode='w', compression=zipfile.ZIP_DEFLATED) as zd:
            order = open(os.path.join(vcs_dir, ".zo")).read().split("\n")
            for name in order:
                conv = self.CONVERTERS[name]
                with zd.open(name, 'w') as z:
                    conv.write_vcs_to_raw(os.path.join(vcs_dir, name), z)

        # write header
        rawzip.write(b'\x00\x00\x00\x00')

        # write zip
        rawzip.write(struct.pack("<i", b.tell()))
        b.seek(0)
        rawzip.write(b.read())

        # write first xml:

        xmlb = XMLConverter(
            'utf-8-sig', True).vcs_to_raw(open(os.path.join(vcs_dir, "3.xml"), 'rb').read())
        rawzip.write(struct.pack("<i", len(xmlb)))
        rawzip.write(xmlb)

        # write second xml:
        xmlb = XMLConverter(
            'utf-8-sig', True).vcs_to_raw(open(os.path.join(vcs_dir, "6.xml"), 'rb').read())
        rawzip.write(struct.pack("<i", len(xmlb) + 34))
        rawzip.write(b'\x00\x00\x00\x00')
        rawzip.write(struct.pack("<i", len(xmlb)))
        rawzip.write(xmlb)

        # write the rest:
        NoopConverter().write_vcs_to_raw(os.path.join(vcs_dir, "7.bytes"), rawzip)

    def write_raw_to_textconv(self, b, outio):
        """ Convert the raw format into readable text for comparison"""

        if b[:4] != b'\x00\x00\x00\x00':
            raise ValueError("TODO")
        len1 = int.from_bytes(b[4:8], byteorder="little")
        start1 = 8
        end1 = start1 + len1
        zip1 = b[start1:end1]
        start2 = end1 + 4
        len2 = int.from_bytes(b[end1:start2], byteorder="little")
        end2 = start2 + len2
        xml1 = b[start2:end2]
        b8 = b[end2:end2+8]
        start3 = end2 + 12
        len3 = int.from_bytes(b[end2 + 8: start3], byteorder="little")
        if int.from_bytes(b[end2:end2+4], "little") - len3 != 34:
            raise ValueError("TODO")
        end3 = start3 + len3
        xml2 = b[start3:end3]
        extra = b[end3:]

        # extract header zip:
        with zipfile.ZipFile(BytesIO(zip1)) as zd:
            order = []
            # read items (in the order they appear in the archive)
            for name in zd.namelist():
                order.append(name)
                print("Filename: " + name, file=outio)
                conv = self.CONVERTERS[name]
                conv.write_raw_to_textconv(zd.read(name), outio)

        # now write the xmls and bytes between:
        # open(os.path.join(outdir, 'DataMashup', "1.int"), 'wb').write(b[4:8])
        print("DataMashup -> XML Block 1", file=outio)
        XMLConverter('utf-8-sig', True).write_raw_to_textconv(xml1, outio)
        print("DataMashup -> XML Block 2", file=outio)
        XMLConverter('utf-8-sig', True).write_raw_to_textconv(xml2, outio)
        print("DataMashup -> Extra Content", file=outio)
        NoopConverter().write_raw_to_textconv(extra, outio)
        print(file=outio)
