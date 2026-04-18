# This is a generated file! Please edit source .ksy file and use kaitai-struct-compiler to rebuild
# type: ignore

import kaitaistruct
from kaitaistruct import KaitaiStruct, KaitaiStream, BytesIO


if getattr(kaitaistruct, 'API_VERSION', (0, 9)) < (0, 11):
    raise Exception("Incompatible Kaitai Struct Python API: 0.11 or later is required, but you have %s" % (kaitaistruct.__version__))

class Dat(KaitaiStruct):
    def __init__(self, _io, _parent=None, _root=None):
        super(Dat, self).__init__(_io)
        self._parent = _parent
        self._root = _root or self
        self._read()

    def _read(self):
        self.header = Dat.Header(self._io, self, self._root)
        self._raw_records = self._io.read_bytes((self.header.file_length - self.header.first_record_position) + 1)
        _io__raw_records = KaitaiStream(BytesIO(self._raw_records))
        self.records = Dat.Records(_io__raw_records, self, self._root)


    def _fetch_instances(self):
        pass
        self.header._fetch_instances()
        self.records._fetch_instances()
        _ = self.data_type_id
        if hasattr(self, '_m_data_type_id'):
            pass

        _ = self.tag_name
        if hasattr(self, '_m_tag_name'):
            pass

        _ = self.tag_name_length
        if hasattr(self, '_m_tag_name_length'):
            pass

        _ = self.third_array_dimension
        if hasattr(self, '_m_third_array_dimension'):
            pass


    class BffbRecord(KaitaiStruct):
        def __init__(self, len_record_buffer, _io, _parent=None, _root=None):
            super(Dat.BffbRecord, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self.len_record_buffer = len_record_buffer
            self._read()

        def _read(self):
            self.record_buffer = self._io.read_bytes(self.len_record_buffer)


        def _fetch_instances(self):
            pass


    class FafaRecord(KaitaiStruct):
        def __init__(self, len_record_buffer, _io, _parent=None, _root=None):
            super(Dat.FafaRecord, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self.len_record_buffer = len_record_buffer
            self._read()

        def _read(self):
            self.record_buffer = self._io.read_bytes(self.len_record_buffer)


        def _fetch_instances(self):
            pass


    class FdfdRecord(KaitaiStruct):
        def __init__(self, len_record_buffer, _io, _parent=None, _root=None):
            super(Dat.FdfdRecord, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self.len_record_buffer = len_record_buffer
            self._read()

        def _read(self):
            self.record_buffer = self._io.read_bytes_full()


        def _fetch_instances(self):
            pass


    class FefeRecord(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(Dat.FefeRecord, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.len_record_buffer = self._io.read_u4le()
            self.blank_1 = self._io.read_u4le()
            self.unknown_1 = self._io.read_u4le()
            self.unknown_2 = self._io.read_u4le()
            self.record_buffer = self._io.read_bytes(self.len_record_buffer)


        def _fetch_instances(self):
            pass


    class Header(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(Dat.Header, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.format_type = self._io.read_u4le()
            self.blank_2 = self._io.read_u4le()
            self.file_length = self._io.read_u4le()
            self.first_record_position = self._io.read_u4le()
            self.blank_3 = self._io.read_u4le()
            self.number_records_fafa = self._io.read_u4le()
            self.header_buffer = []
            for i in range(self.first_record_position - 24):
                self.header_buffer.append(self._io.read_u1())



        def _fetch_instances(self):
            pass
            for i in range(len(self.header_buffer)):
                pass



    class Record(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(Dat.Record, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.identifier = self._io.read_u2le()
            if not  ((self.identifier == 65278) or (self.identifier == 65021) or (self.identifier == 64250) or (self.identifier == 64447)) :
                raise kaitaistruct.ValidationNotAnyOfError(self.identifier, self._io, u"/types/record/seq/0")
            self.len_record = self._io.read_u4le()
            _on = self.identifier
            if _on == 64250:
                pass
                self._raw_record = self._io.read_bytes(self.len_record - 6)
                _io__raw_record = KaitaiStream(BytesIO(self._raw_record))
                self.record = Dat.FafaRecord(self.len_record - 6, _io__raw_record, self, self._root)
            elif _on == 64447:
                pass
                self._raw_record = self._io.read_bytes(self.len_record - 6)
                _io__raw_record = KaitaiStream(BytesIO(self._raw_record))
                self.record = Dat.BffbRecord(self.len_record - 6, _io__raw_record, self, self._root)
            elif _on == 65021:
                pass
                self._raw_record = self._io.read_bytes(self.len_record - 6)
                _io__raw_record = KaitaiStream(BytesIO(self._raw_record))
                self.record = Dat.FdfdRecord(self.len_record - 6, _io__raw_record, self, self._root)
            elif _on == 65278:
                pass
                self._raw_record = self._io.read_bytes(self.len_record - 6)
                _io__raw_record = KaitaiStream(BytesIO(self._raw_record))
                self.record = Dat.FefeRecord(_io__raw_record, self, self._root)
            else:
                pass
                self.record = self._io.read_bytes(self.len_record - 6)


        def _fetch_instances(self):
            pass
            _on = self.identifier
            if _on == 64250:
                pass
                self.record._fetch_instances()
            elif _on == 64447:
                pass
                self.record._fetch_instances()
            elif _on == 65021:
                pass
                self.record._fetch_instances()
            elif _on == 65278:
                pass
                self.record._fetch_instances()
            else:
                pass


    class Records(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(Dat.Records, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.record = []
            i = 0
            while not self._io.is_eof():
                self.record.append(Dat.Record(self._io, self, self._root))
                i += 1



        def _fetch_instances(self):
            pass
            for i in range(len(self.record)):
                pass
                self.record[i]._fetch_instances()



    @property
    def data_type_id(self):
        if hasattr(self, '_m_data_type_id'):
            return self._m_data_type_id

        _pos = self._io.pos()
        self._io.seek(190)
        self._m_data_type_id = self._io.read_u4le()
        self._io.seek(_pos)
        return getattr(self, '_m_data_type_id', None)

    @property
    def tag_name(self):
        if hasattr(self, '_m_tag_name'):
            return self._m_tag_name

        _pos = self._io.pos()
        self._io.seek(240)
        self._m_tag_name = (self._io.read_bytes(self.tag_name_length)).decode(u"UTF-8")
        self._io.seek(_pos)
        return getattr(self, '_m_tag_name', None)

    @property
    def tag_name_length(self):
        if hasattr(self, '_m_tag_name_length'):
            return self._m_tag_name_length

        _pos = self._io.pos()
        self._io.seek(238)
        self._m_tag_name_length = self._io.read_u2le()
        self._io.seek(_pos)
        return getattr(self, '_m_tag_name_length', None)

    @property
    def third_array_dimension(self):
        if hasattr(self, '_m_third_array_dimension'):
            return self._m_third_array_dimension

        _pos = self._io.pos()
        self._io.seek(182)
        self._m_third_array_dimension = self._io.read_u4le()
        self._io.seek(_pos)
        return getattr(self, '_m_third_array_dimension', None)


