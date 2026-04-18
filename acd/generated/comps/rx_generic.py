# This is a generated file! Please edit source .ksy file and use kaitai-struct-compiler to rebuild
# type: ignore

import kaitaistruct
from kaitaistruct import KaitaiStruct, KaitaiStream, BytesIO


if getattr(kaitaistruct, 'API_VERSION', (0, 9)) < (0, 11):
    raise Exception("Incompatible Kaitai Struct Python API: 0.11 or later is required, but you have %s" % (kaitaistruct.__version__))

class RxGeneric(KaitaiStruct):
    def __init__(self, _io, _parent=None, _root=None):
        super(RxGeneric, self).__init__(_io)
        self._parent = _parent
        self._root = _root or self
        self._read()

    def _read(self):
        self.parent_id = self._io.read_u4le()
        self.unique_tag_identifier = self._io.read_u4le()
        self.record_format_version = self._io.read_u2le()
        self.cip_type = self._io.read_u2le()
        self.comment_id = self._io.read_u2le()
        _on = self.cip_type
        if _on == 104:
            pass
            self._raw_main_record = self._io.read_bytes(60)
            _io__raw_main_record = KaitaiStream(BytesIO(self._raw_main_record))
            self.main_record = RxGeneric.RxTag(_io__raw_main_record, self, self._root)
        elif _on == 107:
            pass
            self._raw_main_record = self._io.read_bytes(60)
            _io__raw_main_record = KaitaiStream(BytesIO(self._raw_main_record))
            self.main_record = RxGeneric.RxTag(_io__raw_main_record, self, self._root)
        else:
            pass
            self._raw_main_record = self._io.read_bytes(60)
            _io__raw_main_record = KaitaiStream(BytesIO(self._raw_main_record))
            self.main_record = RxGeneric.Unknown(_io__raw_main_record, self, self._root)
        self.len_record = self._io.read_u4le()
        self.count_record = self._io.read_u4le()
        self.extended_records = []
        for i in range(self.count_record - 1):
            self.extended_records.append(RxGeneric.AttributeRecord(self._io, self, self._root))



    def _fetch_instances(self):
        pass
        _on = self.cip_type
        if _on == 104:
            pass
            self.main_record._fetch_instances()
        elif _on == 107:
            pass
            self.main_record._fetch_instances()
        else:
            pass
            self.main_record._fetch_instances()
        for i in range(len(self.extended_records)):
            pass
            self.extended_records[i]._fetch_instances()

        _ = self.record_buffer
        if hasattr(self, '_m_record_buffer'):
            pass


    class AttributeRecord(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(RxGeneric.AttributeRecord, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.attribute_id = self._io.read_u4le()
            self.len_value = self._io.read_u4le()
            self.value = self._io.read_bytes(self.len_value)


        def _fetch_instances(self):
            pass


    class LastAttributeRecord(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(RxGeneric.LastAttributeRecord, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.attribute_id = self._io.read_u4le()
            self.len_value = self._io.read_u4le()
            self.value = self._io.read_bytes(self.len_value - 4)


        def _fetch_instances(self):
            pass


    class RxMapDevice(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(RxGeneric.RxMapDevice, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            pass


        def _fetch_instances(self):
            pass
            _ = self.module_id
            if hasattr(self, '_m_module_id'):
                pass

            _ = self.parent_module
            if hasattr(self, '_m_parent_module'):
                pass

            _ = self.product_code
            if hasattr(self, '_m_product_code'):
                pass

            _ = self.product_type
            if hasattr(self, '_m_product_type'):
                pass

            _ = self.slot_no
            if hasattr(self, '_m_slot_no'):
                pass

            _ = self.vendor_id
            if hasattr(self, '_m_vendor_id'):
                pass


        @property
        def module_id(self):
            if hasattr(self, '_m_module_id'):
                return self._m_module_id

            _pos = self._io.pos()
            self._io.seek(36)
            self._m_module_id = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_module_id', None)

        @property
        def parent_module(self):
            if hasattr(self, '_m_parent_module'):
                return self._m_parent_module

            _pos = self._io.pos()
            self._io.seek(22)
            self._m_parent_module = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_parent_module', None)

        @property
        def product_code(self):
            if hasattr(self, '_m_product_code'):
                return self._m_product_code

            _pos = self._io.pos()
            self._io.seek(6)
            self._m_product_code = self._io.read_u2le()
            self._io.seek(_pos)
            return getattr(self, '_m_product_code', None)

        @property
        def product_type(self):
            if hasattr(self, '_m_product_type'):
                return self._m_product_type

            _pos = self._io.pos()
            self._io.seek(4)
            self._m_product_type = self._io.read_u2le()
            self._io.seek(_pos)
            return getattr(self, '_m_product_type', None)

        @property
        def slot_no(self):
            if hasattr(self, '_m_slot_no'):
                return self._m_slot_no

            _pos = self._io.pos()
            self._io.seek(32)
            self._m_slot_no = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_slot_no', None)

        @property
        def vendor_id(self):
            if hasattr(self, '_m_vendor_id'):
                return self._m_vendor_id

            _pos = self._io.pos()
            self._io.seek(2)
            self._m_vendor_id = self._io.read_u2le()
            self._io.seek(_pos)
            return getattr(self, '_m_vendor_id', None)


    class RxTag(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(RxGeneric.RxTag, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            pass


        def _fetch_instances(self):
            pass
            _ = self.cip_data_type
            if hasattr(self, '_m_cip_data_type'):
                pass

            _ = self.data_table_instance
            if hasattr(self, '_m_data_table_instance'):
                pass

            _ = self.data_type
            if hasattr(self, '_m_data_type'):
                pass

            _ = self.dimension_1
            if hasattr(self, '_m_dimension_1'):
                pass

            _ = self.dimension_2
            if hasattr(self, '_m_dimension_2'):
                pass

            _ = self.dimension_3
            if hasattr(self, '_m_dimension_3'):
                pass

            _ = self.external_access
            if hasattr(self, '_m_external_access'):
                pass

            _ = self.radix
            if hasattr(self, '_m_radix'):
                pass


        @property
        def cip_data_type(self):
            if hasattr(self, '_m_cip_data_type'):
                return self._m_cip_data_type

            _pos = self._io.pos()
            self._io.seek(52)
            self._m_cip_data_type = self._io.read_u2le()
            self._io.seek(_pos)
            return getattr(self, '_m_cip_data_type', None)

        @property
        def data_table_instance(self):
            if hasattr(self, '_m_data_table_instance'):
                return self._m_data_table_instance

            _pos = self._io.pos()
            self._io.seek(36)
            self._m_data_table_instance = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_data_table_instance', None)

        @property
        def data_type(self):
            if hasattr(self, '_m_data_type'):
                return self._m_data_type

            _pos = self._io.pos()
            self._io.seek(28)
            self._m_data_type = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_data_type', None)

        @property
        def dimension_1(self):
            if hasattr(self, '_m_dimension_1'):
                return self._m_dimension_1

            _pos = self._io.pos()
            self._io.seek(12)
            self._m_dimension_1 = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_dimension_1', None)

        @property
        def dimension_2(self):
            if hasattr(self, '_m_dimension_2'):
                return self._m_dimension_2

            _pos = self._io.pos()
            self._io.seek(16)
            self._m_dimension_2 = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_dimension_2', None)

        @property
        def dimension_3(self):
            if hasattr(self, '_m_dimension_3'):
                return self._m_dimension_3

            _pos = self._io.pos()
            self._io.seek(20)
            self._m_dimension_3 = self._io.read_u4le()
            self._io.seek(_pos)
            return getattr(self, '_m_dimension_3', None)

        @property
        def external_access(self):
            if hasattr(self, '_m_external_access'):
                return self._m_external_access

            _pos = self._io.pos()
            self._io.seek(34)
            self._m_external_access = self._io.read_u2le()
            self._io.seek(_pos)
            return getattr(self, '_m_external_access', None)

        @property
        def radix(self):
            if hasattr(self, '_m_radix'):
                return self._m_radix

            _pos = self._io.pos()
            self._io.seek(32)
            self._m_radix = self._io.read_u2le()
            self._io.seek(_pos)
            return getattr(self, '_m_radix', None)

        @property
        def valid(self):
            if hasattr(self, '_m_valid'):
                return self._m_valid

            self._m_valid = True
            return getattr(self, '_m_valid', None)


    class Unknown(KaitaiStruct):
        def __init__(self, _io, _parent=None, _root=None):
            super(RxGeneric.Unknown, self).__init__(_io)
            self._parent = _parent
            self._root = _root
            self._read()

        def _read(self):
            self.body = self._io.read_bytes(60)


        def _fetch_instances(self):
            pass


    @property
    def record_buffer(self):
        if hasattr(self, '_m_record_buffer'):
            return self._m_record_buffer

        _pos = self._io.pos()
        self._io.seek(14)
        self._m_record_buffer = self._io.read_bytes(60)
        self._io.seek(_pos)
        return getattr(self, '_m_record_buffer', None)


