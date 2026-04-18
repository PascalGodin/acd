import html
import os
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from sqlite3 import Cursor
from typing import List, Tuple, Dict, Union

from acd.generated.comps.rx_generic import RxGeneric
from acd.l5x.catalog_numbers import CATALOG_NUMBERS
from acd.l5x.port_structures import PORT_STRUCTURES


@dataclass
class L5xElementBuilder:
    _cur: Cursor
    _object_id: int = -1


# Maps Python attribute names to L5X XML section wrapper tag names.
# Entries here also control which list attributes are serialized as child sections.
_LIST_SECTION_NAMES = {
    "tags": "Tags",
    "data_types": "DataTypes",
    "members": "Members",
    "modules": "Modules",
    "programs": "Programs",
    "routines": "Routines",
    "aois": "AddOnInstructionDefinitions",
    "tasks": "Tasks",
    "scheduled_programs": "ScheduledPrograms",
}


@dataclass
class L5xElement:
    _name: str

    def __post_init__(self):
        self._export_name = ""

    def to_xml(self) -> str:
        attribute_list: List[str] = []
        child_list: List[str] = []
        for attribute in self.__dict__:
            if attribute[0] != "_":
                attribute_value = self.__getattribute__(attribute)
                if attribute_value is None:
                    continue
                if isinstance(attribute_value, L5xElement):
                    child_list.append(attribute_value.to_xml())
                elif isinstance(attribute_value, list):
                    if attribute in _LIST_SECTION_NAMES:
                        section_name = _LIST_SECTION_NAMES[attribute]
                        new_child_list: List[str] = []
                        for element in attribute_value:
                            if isinstance(element, L5xElement):
                                if getattr(element, "_l5x_exclude", False):
                                    continue
                                new_child_list.append(element.to_xml())
                            else:
                                new_child_list.append(f"<{element}/>")
                        child_list.append(
                            f'<{section_name}>{"".join(new_child_list)}</{section_name}>'
                        )
                else:
                    if attribute == "cls":
                        attribute = "class"
                    if isinstance(attribute_value, bool):
                        attribute_value = str(attribute_value).lower()
                    _overrides = getattr(self, "_xml_attr_overrides", {})
                    xml_attr_name = _overrides.get(attribute, attribute.title().replace("_", ""))
                    attribute_list.append(
                        f'{xml_attr_name}="{html.escape(str(attribute_value), quote=True)}"'
                    )

        _export_name = (
            getattr(self, "_export_name", "") or self.__class__.__name__.title().replace("_", "")
        )
        return f'<{_export_name} {" ".join(attribute_list)}>{"".join(child_list)}</{_export_name}>'


@dataclass
class Member(L5xElement):
    name: str
    data_type: str
    dimension: int
    radix: str
    hidden: bool
    external_access: str


@dataclass
class DataType(L5xElement):
    name: str
    family: str
    cls: str
    members: List[Member]

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "DataType"

    @property
    def _l5x_exclude(self) -> bool:
        return self.cls == "ProductDefined"


@dataclass
class Tag(L5xElement):
    name: str
    tag_type: str
    data_type: str
    radix: str
    external_access: str
    _data_table_instance: int
    _comments: List[Tuple[str, str]]

    @property
    def _l5x_exclude(self) -> bool:
        """Exclude tags with empty or non-identifier names (hex-address placeholders, etc.)."""
        return not self.name or not (self.name[0].isalpha() or self.name[0] == "_")


@dataclass
class Module(L5xElement):
    """Represents a Logix hardware module (<Module> in L5X)."""
    name: str
    catalog_number: str
    vendor: int
    product_type: int
    product_code: int
    major: int
    minor: int
    parent_module: str
    parent_mod_port_id: int
    inhibited: str
    major_fault: str
    # Private fields (not serialised as XML attributes)
    _ekey_state: str = field(default="CompatibleModule")
    _slot: int = field(default=0)
    _ip_address: str = field(default="")
    _backplane_slot: Union[int, None] = field(default=None)
    _chassis_size: Union[int, None] = field(default=None)
    _port_child_counts: Dict[int, int] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "Module"

    def to_xml(self) -> str:
        # Hash-named drive peripherals have no Name attribute in Logix-exported L5X.
        name_attr = "" if self.name == "?" else f'Name="{self.name}" '
        attrs = (
            f'{name_attr}'
            f'CatalogNumber="{self.catalog_number}" '
            f'Vendor="{self.vendor}" '
            f'ProductType="{self.product_type}" '
            f'ProductCode="{self.product_code}" '
            f'Major="{self.major}" '
            f'Minor="{self.minor}" '
            f'ParentModule="{self.parent_module}" '
            f'ParentModPortId="{self.parent_mod_port_id}" '
            f'Inhibited="{self.inhibited}" '
            f'MajorFault="{self.major_fault}"'
        )
        ekey = f'<EKey State="{self._ekey_state}"/>'
        ports = self._build_ports_xml()
        return f'<Module {attrs}>{ekey}{ports}</Module>'

    def _build_ports_xml(self) -> str:
        """Build the <Ports>...</Ports> XML section for this module.

        Looks up the port structure from PORT_STRUCTURES by (vendor, product_type,
        product_code). Falls back to <Ports/> if the catalog number is not in the table.
        """
        key = (self.vendor, self.product_type, self.product_code)
        port_defs = PORT_STRUCTURES.get(key)
        if port_defs is None:
            return '<Ports/>'

        is_root = (self.major_fault == "true")
        port_parts: List[str] = []

        for pd in port_defs:
            # --- Upstream direction ---
            # Root modules (self-parenting CPU) have all ports downstream.
            # For other modules: if upstream_fixed=True, use the static upstream_port value.
            # If upstream_fixed=False, determine from parent_mod_port_id (the port on the
            # parent module that this module connects through — when that matches port_id,
            # this port faces upstream).
            if is_root:
                upstream_str = "false"
            elif pd.upstream_fixed:
                upstream_str = "true" if pd.upstream_port else "false"
            else:
                upstream_str = "true" if pd.port_id == self.parent_mod_port_id else "false"

            # --- Address attribute ---
            if pd.address_mode == "omit":
                addr_attr = ""
            elif pd.address_mode == "slot":
                # Non-upstream ICP ports (remote chassis owner): use _backplane_slot if known.
                if upstream_str == "false" and self._backplane_slot is not None:
                    addr_attr = f' Address="{self._backplane_slot}"'
                else:
                    addr_attr = f' Address="{self._slot}"'
            elif pd.address_mode == "zero":
                addr_attr = ' Address="0"'
            else:  # "empty" — use IP from binary if present, else omit value
                addr_attr = f' Address="{self._ip_address}"'

            # --- Bus element ---
            # Bus is only emitted on downstream (Upstream="false") ports.
            # upstream ports never carry a Bus element.
            is_upstream = (upstream_str == "true")
            bus_xml = self._bus_xml(pd, is_upstream)

            if bus_xml:
                port_parts.append(
                    f'<Port Id="{pd.port_id}"{addr_attr} Type="{pd.port_type}" Upstream="{upstream_str}">\n'
                    f'{bus_xml}\n'
                    f'</Port>\n'
                )
            else:
                port_parts.append(
                    f'<Port Id="{pd.port_id}"{addr_attr} Type="{pd.port_type}" Upstream="{upstream_str}"/>\n'
                )

        return f'<Ports>\n{"".join(port_parts)}</Ports>\n'

    def _bus_xml(self, pd, is_upstream: bool) -> str:
        """Return the Bus XML string for a port, or '' if no Bus element should be emitted.

        Bus elements are only present on downstream (Upstream=false) ports.
        """
        if is_upstream:
            return ""
        mode = pd.bus_mode
        if mode == "none":
            return ""
        if mode == "always":
            return "<Bus/>"
        if mode.startswith("fixed:"):
            size = mode.split(":")[1]
            return f'<Bus Size="{size}"/>'
        if mode == "children_or_none":
            child_count = self._port_child_counts.get(pd.port_id, 0)
            if self._chassis_size is not None:
                child_count = max(child_count, self._chassis_size)
            if child_count == 0:
                return ""
            return f'<Bus Size="{child_count}"/>'
        # "children" mode: child count, but never less than _chassis_size when known
        # (handles remote chassis with empty slots not represented as child modules).
        child_count = self._port_child_counts.get(pd.port_id, 0)
        if self._chassis_size is not None:
            child_count = max(child_count, self._chassis_size)
        return f'<Bus Size="{child_count}"/>'


@dataclass
class Routine(L5xElement):
    name: str
    type: str
    rungs: List[str]
    _rung_ids: List[int] = field(default_factory=list)


@dataclass
class AOI(L5xElement):
    name: str
    revision: str
    revision_extension: Union[str, None]  # None if absent (omitted from XML)
    vendor: Union[str, None]  # None if absent (omitted from XML)
    execute_prescan: str
    execute_postscan: str
    execute_enable_in_false: str
    created_date: str
    created_by: str
    edited_date: str
    edited_by: str
    software_revision: str
    routines: List[Routine]
    tags: List[Tag]

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "AddOnInstructionDefinition"


@dataclass
class Program(L5xElement):
    name: str
    test_edits: str
    main_routine_name: Union[str, None]  # None if absent (omitted from XML)
    fault_routine_name: Union[str, None]  # None if absent (omitted from XML)
    disabled: str
    use_as_folder: str
    routines: List[Routine]
    tags: List[Tag]


@dataclass
class ScheduledProgram(L5xElement):
    name: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "ScheduledProgram"


@dataclass
class EventInfo(L5xElement):
    event_trigger: str
    enable_timeout: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "EventInfo"


@dataclass
class Task(L5xElement):
    name: str
    type: str
    rate: Union[str, None]  # None for CONTINUOUS tasks (omitted from XML)
    priority: str
    watchdog: str
    disable_update_outputs: str
    inhibit_task: str
    event_info: Union[EventInfo, None]  # None for non-EVENT tasks
    scheduled_programs: List[ScheduledProgram]


@dataclass
class Controller(L5xElement):
    use: str
    name: str
    processor_type: Union[str, None]  # None if unknown (omitted from XML)
    major_rev: str
    minor_rev: str
    major_fault_program: Union[str, None]  # None if not set (omitted from XML)
    project_creation_date: str
    last_modified_date: str
    sfc_execution_control: str
    sfc_restart_position: str
    sfc_last_scan: str
    project_sn: str
    match_project_to_controller: str
    can_use_rpi_from_producer: str
    inhibit_automatic_firmware_update: str
    pass_through_configuration: str
    download_project_documentation_and_extended_properties: str
    download_project_custom_properties: str
    report_minor_overflow: str
    auto_diags_enabled: str
    web_server_enabled: str
    data_types: List[DataType]
    modules: List[Module]
    tags: List[Tag]
    programs: List[Program]
    tasks: List[Task]
    aois: List[AOI]

    def __post_init__(self):
        super().__post_init__()
        self._xml_attr_overrides = {
            "sfc_execution_control": "SFCExecutionControl",
            "sfc_restart_position": "SFCRestartPosition",
            "sfc_last_scan": "SFCLastScan",
            "project_sn": "ProjectSN",
            "can_use_rpi_from_producer": "CanUseRPIFromProducer",
        }

    def to_xml(self) -> str:
        base = super().to_xml()
        # Split at the end of the opening <Controller ...> tag so we can inject
        # structural stubs before the data sections and post-sections after them.
        idx = base.index(">")
        open_tag = base[: idx + 1]
        inner = base[idx + 1 : -len("</Controller>")]
        pre = (
            '<RedundancyInfo Enabled="false" KeepTestEditsOnSwitchOver="false"/>'
            '<Security Code="0" ChangesToDetect="16#ffff_ffff"/>'
            '<SafetyInfo/>'
        )
        post = (
            '<CST MasterID="0"/>'
            '<WallClockTime LocalTimeAdjustment="0" TimeZone="0"/>'
            '<Trends/>'
            '<DataLogs/>'
            '<TimeSynchronize Priority1="128" Priority2="128" PTPEnable="true"/>'
            '<EthernetPorts><EthernetPort Port="1" Label="1" PortEnabled="true"/></EthernetPorts>'
        )
        return open_tag + pre + inner + post + "</Controller>"


@dataclass
class RSLogix5000Content(L5xElement):
    """Controller Project"""

    controller: Union[Controller, None]
    schema_revision: str
    software_revision: str
    target_name: str
    target_type: str
    contains_context: str
    export_date: str
    export_options: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "RSLogix5000Content"


def radix_enum(i: int) -> str:
    if i == 0:
        return "NullType"
    if i == 1:
        return "General"
    if i == 2:
        return "Binary"
    if i == 3:
        return "Octal"
    if i == 4:
        return "Decimal"
    if i == 5:
        return "Hex"
    if i == 6:
        return "Exponential"
    if i == 7:
        return "Float"
    if i == 8:
        return "ASCII"
    if i == 9:
        return "Unicode"
    if i == 10:
        return "Date/Time"
    if i == 11:
        return "Date/Time (ns)"
    if i == 12:
        return "UseTypeStyle"
    return "General"


def external_access_enum(i: int) -> str:
    default = "Read/Write"
    if i == 0:
        return default
    if i == 2:
        return "Read Only"
    if i == 3:
        return "None"
    return default


@dataclass
class MemberBuilder(L5xElementBuilder):
    record: bytes = field(default_factory=bytes)

    def build(self) -> Member:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        name = results[0][0]
        r = RxGeneric.from_bytes(results[0][3])
        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return Member(name, name, "", 0, "Decimal", False, "Read/Write")

        extended_records: Dict[int, List[int]] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = extended_record.value

        cip_data_typoe = struct.unpack_from("<I", self.record, 0x78)[0]
        dimension = struct.unpack_from("<I", self.record, 0x5C)[0]
        radix = radix_enum(struct.unpack_from("<I", self.record, 0x54)[0])
        data_type_id = struct.unpack_from("<I", self.record, 0x58)[0]
        hidden = bool(struct.unpack_from("<I", self.record, 0x70)[0])
        external_access = external_access_enum(
            struct.unpack_from("<I", self.record, 0x74)[0]
        )

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(data_type_id)
        )
        data_type_results = self._cur.fetchall()
        data_type = data_type_results[0][0]

        return Member(name, name, data_type, dimension, radix, hidden, external_access)


@dataclass
class DataTypeBuilder(L5xElementBuilder):
    def build(self) -> DataType:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        name = results[0][0]

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return DataType(name, name, "NoFamily", "User", [])

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        string_family_int = struct.unpack("<I", extended_records[0x6C])[0]
        string_family = "StringFamily" if string_family_int == 1 else "NoFamily"

        built_in = struct.unpack("<I", extended_records[0x67])[0]
        module_defined = struct.unpack("<I", extended_records[0x69])[0]

        class_type = "User"
        if module_defined > 0:
            class_type = "IO"
        if built_in & 0x03:
            class_type = "ProductDefined"
        if 0x64 in extended_records and len(extended_records[0x64]) == 0x04:
            member_count = struct.unpack("<I", extended_records[0x64])[0]
        else:
            member_count = 0

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
        )
        member_results = self._cur.fetchall()
        children: List[Member] = []
        if len(member_results) == 1:
            member_collection_id = member_results[0][1]

            self._cur.execute(
                f"SELECT comp_name, object_id, parent_id, seq_number, record FROM comps WHERE parent_id={member_collection_id} ORDER BY seq_number"
            )
            children_results = self._cur.fetchall()

            # Some ACD files have mismatched member_count vs children list — iterate what we have
            for idx, child in enumerate(children_results):
                key = 0x6E + idx
                if key not in extended_records:
                    break
                try:
                    children.append(
                        MemberBuilder(
                            self._cur, child[1], bytes(extended_records[key])
                        ).build()
                    )
                except Exception:
                    pass

        return DataType(name, name, string_family, class_type, children)


@dataclass
class ModuleBuilder(L5xElementBuilder):
    # Map from modid (u32) → module name, built by ControllerBuilder and passed in.
    _modid_to_name: Dict[int, str] = field(default_factory=dict)

    def _ip_from_data_collection(self, icp_slot: int) -> str:
        """Look up the Ethernet IP for a local backplane module via RxDataCollection.

        Local bridge modules (e.g. EN2T in the main chassis) store their IP as XML
        in hash-named children of RxDataCollection. The record for a given module
        contains its ICP slot as Type="ICP" Addr="{slot}", which uniquely identifies it.
        """
        import re as _re
        needle = f'Type="ICP" Addr="{icp_slot}"'.encode()
        # Find RxDataCollection — it is a direct child of the controller object.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return ""
        coll_oid = row[0]
        # Fetch all children in batches and filter in Python (SQLite LIKE on BLOBs is unreliable).
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        for (raw,) in self._cur.fetchall():
            raw = bytes(raw)
            if needle not in raw:
                continue
            m = _re.search(rb'Type="EN" Addr="([^"]+)"', raw)
            return m.group(1).decode("ascii", errors="replace") if m else ""
        return ""

    def build(self) -> Module:
        self._cur.execute(
            "SELECT comp_name, object_id, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        db_name = row[0]
        raw_rec = bytes(row[2])

        # Hex-encoded names like $02cc5e9d$ are unnamed peripheral modules (drive expansion
        # cards, etc.).  Logix Designer exports these with Name="?".
        name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name

        try:
            r = RxGeneric.from_bytes(raw_rec)
        except Exception:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        if r.cip_type != 0x69:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        exts: Dict[int, bytes] = {er.attribute_id: bytes(er.value) for er in r.extended_records}
        e1 = exts.get(0x001, b"")
        if len(e1) < 0x30:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        vendor        = struct.unpack("<H", e1[0x02:0x04])[0]
        product_type  = struct.unpack("<H", e1[0x04:0x06])[0]
        product_code  = struct.unpack("<H", e1[0x06:0x08])[0]
        # bit 7 of the major byte is a flag; strip it to get the firmware revision.
        major         = e1[0x08] & 0x7F
        minor         = e1[0x09]
        parent_modid  = struct.unpack("<I", e1[0x16:0x1A])[0]
        parent_port   = struct.unpack("<H", e1[0x1A:0x1C])[0]
        slot          = struct.unpack("<I", e1[0x1C:0x20])[0]

        # Hash-named modules are drive peripheral expansion cards. The ACD binary stores
        # them with ProductType=123 and site-specific ProductCodes, but Logix exports them
        # all as PT=0 PC=28 (RHINOBP-DRIVE-PERIPHERAL-MODULE) without a Name attribute.
        if name == "?":
            product_type = 0
            product_code = 28

        # Resolve parent module name from the modid→name map built by ControllerBuilder.
        parent_name = self._modid_to_name.get(parent_modid, "Local")

        own_modid   = struct.unpack("<I", e1[0x2C:0x30])[0]
        # MajorFault=true only for the root controller module (self-referential parent).
        major_fault = "true" if parent_modid == own_modid else "false"
        # bit 2 (0x04) of e1[0] → EKey Disabled (Local=0x06→Disabled, EN2T=0x11→CompatibleModule).
        ekey_state  = "Disabled" if (e1[0] & 0x04) else "CompatibleModule"

        # IP address: stored at e1[0x30] as a u16 length-prefixed ASCII string for modules
        # that connect via Ethernet upstream (parent_port == 2). Local backplane bridge
        # modules (parent_port == 1, e.g. local EN2T) leave e1[0x32] zero — their IP is
        # stored as XML in a child of RxDataCollection, keyed by ICP slot number.
        ip_address = ""
        if len(e1) > 0x32:
            ip_len = struct.unpack("<H", e1[0x30:0x32])[0]
            if ip_len:
                ip_address = e1[0x32:0x32 + ip_len].rstrip(b"\x00").decode("ascii", errors="replace")
        if not ip_address and slot:
            ip_address = self._ip_from_data_collection(slot)

        # For modules that own a remote backplane (e.g. remote chassis EN2T), the Output
        # connection record under RxMapConnectionCollection stores the chassis size at [0x4e]
        # and the module's own slot in that chassis at [0x6e].
        backplane_slot = None
        chassis_size = None
        self._cur.execute(
            "SELECT o.record FROM comps coll "
            "JOIN comps o ON o.parent_id = coll.object_id AND o.comp_name = 'Output' "
            "WHERE coll.parent_id = ? AND coll.comp_name = 'RxMapConnectionCollection'",
            (self._object_id,),
        )
        out_row = self._cur.fetchone()
        if out_row:
            out_rec = bytes(out_row[0])
            if len(out_rec) > 0x70:
                backplane_slot = struct.unpack("<H", out_rec[0x6e:0x70])[0]
                chassis_size   = struct.unpack("<H", out_rec[0x4e:0x50])[0]

        return Module(
            name,           # L5xElement._name (private)
            name,           # Module.name
            CATALOG_NUMBERS.get((vendor, product_type, product_code), ""),
            vendor,
            product_type,
            product_code,
            major,
            minor,
            parent_name,
            parent_port,
            "false",        # Inhibited: always false in practice; no known bit
            major_fault,
            _ekey_state=ekey_state,
            _slot=slot,
            _ip_address=ip_address,
            _backplane_slot=backplane_slot,
            _chassis_size=chassis_size,
        )


@dataclass
class TagBuilder(L5xElementBuilder):
    def build(self) -> Tag:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return Tag(
                results[0][0], results[0][0], "Base", "", "Decimal", "None", 0, []
            )

        if r.cip_type != 0x6B and r.cip_type != 0x68:
            return Tag(
                results[0][0], results[0][0], "Base", "", "Decimal", "None", 0, []
            )
        if r.main_record.data_type == 0xFFFFFFFF:
            data_type = ""
        else:
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
                + str(r.main_record.data_type)
            )
            data_type_results = self._cur.fetchall()
            data_type = data_type_results[0][0]

        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        if 0x01 not in extended_records:
            return Tag(results[0][0], results[0][0], "Base", data_type, "Decimal", "None", 0, comment_results)
        name_length = struct.unpack("<H", extended_records[0x01][0:2])[0]
        name = bytes(extended_records[0x01][2 : name_length + 2]).decode("utf-8", errors="replace")

        radix = radix_enum(r.main_record.radix)
        try:
            name_length_raw = struct.unpack_from("<H", extended_records[0x01], 0x21E)[0]
            external_access = external_access_enum(name_length_raw)
        except Exception:
            external_access = "None"

        if r.main_record.dimension_1 != 0:
            data_type = data_type + "[" + str(r.main_record.dimension_1) + "]"
        if r.main_record.dimension_2 != 0:
            data_type = data_type + "[" + str(r.main_record.dimension_2) + "]"
        if r.main_record.dimension_3 != 0:
            data_type = data_type + "[" + str(r.main_record.dimension_3) + "]"
        return Tag(
            name,
            name,
            "Base",
            data_type,
            radix,
            external_access,
            r.main_record.data_table_instance,
            comment_results,
        )


def routine_type_enum(idx: int) -> str:
    if idx == 0:
        return "TypeLess"
    if idx == 1:
        return "RLL"
    if idx == 2:
        return "FBD"
    if idx == 3:
        return "SFC"
    if idx == 4:
        return "ST"
    if idx == 5:
        return "External"
    if idx == 6:
        return "Encrypted"
    return "Typeless"


@dataclass
class RoutineBuilder(L5xElementBuilder):
    def build(self) -> Routine:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return Routine(results[0][0], results[0][0], "", [])

        record = results[0][3]
        name = results[0][0]
        routine_type = routine_type_enum(
            struct.unpack_from("<H", r.record_buffer, 0x30)[0]
        )

        self._cur.execute(
            "SELECT rm.object_id, r.rung FROM region_map rm "
            "LEFT JOIN rungs r ON r.object_id = rm.object_id "
            "WHERE rm.parent_id=" + str(self._object_id) + " ORDER BY rm.seq_no"
        )
        rows = [(row[0], row[1]) for row in self._cur.fetchall() if row[1] is not None]
        rung_ids = [row[0] for row in rows]
        rungs = [row[1] for row in rows]
        return Routine(name, name, routine_type, rungs, rung_ids)


def _parse_fffeff(data: bytes, offset: int):
    """Parse one fffeff-encoded string at offset. Returns (str, new_offset)."""
    if offset + 3 > len(data) or not (data[offset] == 0xFF and data[offset+1] == 0xFE and data[offset+2] == 0xFF):
        return "", offset
    length = data[offset+3]
    s = data[offset+4:offset+4+length*2].decode("utf-16-le", errors="replace")
    return s, offset + 4 + length * 2


def _parse_aoi_nameless(data: bytes) -> dict:
    """Extract AOI metadata from its large nameless record."""
    result: dict = {}

    offset = 0x1A
    # Three empty fffeff strings
    for _ in range(3):
        _, offset = _parse_fffeff(data, offset)

    # 8 bytes (unknown - some kind of date, skip)
    offset += 8

    # 2-byte constant (0x0002 observed)
    offset += 2

    # CreatedBy
    result["created_by"], offset = _parse_fffeff(data, offset)

    # Software revision at creation time (skip - we want the current one later)
    _, offset = _parse_fffeff(data, offset)

    # 4 zero bytes
    offset += 4

    # Empty fffeff placeholder
    _, offset = _parse_fffeff(data, offset)

    # CreatedDate FILETIME (8 bytes, Windows FILETIME in 100-ns units)
    ft = struct.unpack_from("<Q", data, offset)[0]
    if ft:
        dt = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
        result["created_date"] = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    else:
        result["created_date"] = ""
    offset += 8

    # EditedBy
    result["edited_by"], offset = _parse_fffeff(data, offset)

    # SoftwareRevision (current)
    result["software_revision"], offset = _parse_fffeff(data, offset)

    # 4 bytes (01 00 00 00)
    offset += 4

    # RevisionExtension
    rev_ext, offset = _parse_fffeff(data, offset)
    result["revision_extension"] = rev_ext or None

    # EditedDate FILETIME (always last 8 bytes)
    ft = struct.unpack_from("<Q", data, len(data) - 8)[0]
    if ft:
        dt = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
        result["edited_date"] = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    else:
        result["edited_date"] = ""

    return result


@dataclass
class AoiBuilder(L5xElementBuilder):
    def build(self) -> AOI:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        aoi_record = bytes(results[0][3])
        name = results[0][0]

        # --- Revision (major.minor) from ext[0x01] ---
        try:
            r = RxGeneric.from_bytes(aoi_record)
            exts: Dict[int, bytes] = {e.attribute_id: bytes(e.value) for e in r.extended_records}
            e01 = exts.get(0x01, b"")
            rev_major = struct.unpack_from("<H", e01, 0x1A)[0] if len(e01) > 0x1B else 1
            rev_minor = struct.unpack_from("<H", e01, 0x1C)[0] if len(e01) > 0x1D else 0
        except Exception:
            rev_major, rev_minor = 1, 0
        revision = f"{rev_major}.{rev_minor}"

        # --- Vendor from comps record ---
        vlen = struct.unpack_from("<H", aoi_record, 0xA6)[0] if len(aoi_record) > 0xA8 else 0
        vendor: Union[str, None] = aoi_record[0xA8:0xA8+vlen].decode("utf-8", errors="replace") if vlen > 0 else None

        # --- Metadata from large nameless record ---
        self._cur.execute(
            "SELECT record FROM nameless WHERE parent_id=" + str(self._object_id)
            + " ORDER BY LENGTH(record) DESC LIMIT 1"
        )
        nameless_row = self._cur.fetchone()
        if nameless_row and len(bytes(nameless_row[0])) > 50:
            meta = _parse_aoi_nameless(bytes(nameless_row[0]))
        else:
            meta = {"created_by": "", "created_date": "", "edited_by": "", "edited_date": "",
                    "software_revision": "", "revision_extension": None}

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxRoutineCollection'"
        )
        collection_results = self._cur.fetchall()
        routines: List[Routine] = []
        tags: List[Tag] = []
        if len(collection_results) != 0:
            collection_id = collection_results[0][1]
        else:
            return AOI(name, name, revision, meta["revision_extension"], vendor,
                       "false", "false", "false",
                       meta["created_date"], meta["created_by"],
                       meta["edited_date"], meta["edited_by"],
                       meta["software_revision"], routines, tags)

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(collection_id)
        )
        routine_results = self._cur.fetchall()

        for child in routine_results:
            routines.append(RoutineBuilder(self._cur, child[1]).build())

        # Get the AOI-Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        tag_coll = self._cur.fetchall()
        if len(tag_coll) > 1:
            raise Exception("Contains more than one AOI tag collection")

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(tag_coll[0][1])
        )
        for result in self._cur.fetchall():
            tags.append(TagBuilder(self._cur, result[1]).build())

        return AOI(
            name, name, revision,
            meta["revision_extension"],
            vendor,
            "false", "false", "false",
            meta["created_date"], meta["created_by"],
            meta["edited_date"], meta["edited_by"],
            meta["software_revision"],
            routines, tags,
        )


@dataclass
class ProgramBuilder(L5xElementBuilder):
    def build(self) -> Program:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        prog_record = bytes(results[0][3])
        r = RxGeneric.from_bytes(prog_record)

        name = results[0][0]

        # --- MainRoutineName and FaultRoutineName from extended records ---
        # ext[0x12D] = MainRoutine object_id, ext[0x066] = FaultRoutine object_id
        exts: Dict[int, bytes] = {e.attribute_id: bytes(e.value) for e in r.extended_records}
        main_routine_name: Union[str, None] = None
        fault_routine_name: Union[str, None] = None
        if 0x12D in exts and len(exts[0x12D]) >= 4:
            main_oid = struct.unpack_from("<I", exts[0x12D], 0)[0]
            if main_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(main_oid))
                row = self._cur.fetchone()
                main_routine_name = row[0] if row else None
        if 0x066 in exts and len(exts[0x066]) >= 4:
            fault_oid = struct.unpack_from("<I", exts[0x066], 0)[0]
            if fault_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(fault_oid))
                row = self._cur.fetchone()
                fault_routine_name = row[0] if row else None

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxRoutineCollection'"
        )
        collection_results = self._cur.fetchall()
        collection_id = collection_results[0][1]

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(collection_id)
        )
        routine_results = self._cur.fetchall()

        routines = []
        for child in routine_results:
            routines.append(RoutineBuilder(self._cur, child[1]).build())

        # Get the Program Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one program tag collection")

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(results[0][1])
        )
        results = self._cur.fetchall()
        tags: List[Tag] = []
        for result in results:
            tags.append(TagBuilder(self._cur, result[1]).build())

        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        return Program(name, name, "false", main_routine_name, fault_routine_name,
                       "false", "false", routines, tags)


_TASK_TYPE_MAP = {1: "EVENT", 2: "PERIODIC", 4: "CONTINUOUS"}


@dataclass
class TaskBuilder(L5xElementBuilder):
    def build(self, comment_id_to_program: Dict[int, str]) -> Task:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name, record = row[0], row[1]

        # All task config fields live within ext[0x01], accessed via absolute BLOB offsets.
        # These offsets were reverse-engineered from CIPDemo_RevEng.ACD.
        rate_us = struct.unpack_from("<I", record, 0x106C)[0]
        type_val = struct.unpack_from("<H", record, 0x10F6)[0]
        priority = struct.unpack_from("<H", record, 0x10F8)[0]
        watchdog_us = struct.unpack_from("<I", record, 0x110A)[0]
        disable_update = record[0x112E]

        task_type = _TASK_TYPE_MAP.get(type_val, "PERIODIC")
        rate_str = str(rate_us // 1000) if task_type != "CONTINUOUS" else None

        # Scheduled programs: ext[0x01] value starts at BLOB offset 0x5A.
        # Format: u16 count followed by N u32 comment_ids.
        prog_count = struct.unpack_from("<H", record, 0x5A)[0]
        scheduled_programs = []
        for i in range(prog_count):
            cid = struct.unpack_from("<I", record, 0x5A + 2 + i * 4)[0]
            prog_name = comment_id_to_program.get(cid)
            if prog_name:
                scheduled_programs.append(ScheduledProgram(prog_name, prog_name))

        event_info = None
        if task_type == "EVENT":
            event_info = EventInfo("EventInfo", "EVENT Instruction Only", "false")

        return Task(
            name,
            name,
            task_type,
            rate_str,
            str(priority),
            str(watchdog_us // 1000),
            "true" if disable_update else "false",
            "false",
            event_info,
            scheduled_programs,
        )


@dataclass
class ControllerBuilder(L5xElementBuilder):
    def build(self) -> Controller:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id=0 AND record_type=256"
        )
        results = self._cur.fetchall()
        if len(results) != 1:
            raise Exception("Does not contain exactly one root controller node")

        r = RxGeneric.from_bytes(results[0][4])
        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        def _decode_utf16(key):
            raw = extended_records.get(key)
            if raw is None or len(raw) < 2:
                return ""
            return bytes(raw[:-2]).decode("utf-16", errors="replace")

        sfc_execution_control = _decode_utf16(0x6F)
        sfc_restart_position = _decode_utf16(0x70)
        sfc_last_scan = _decode_utf16(0x71)

        if 0x75 in extended_records:
            sn_raw = hex(struct.unpack("<I", extended_records[0x75])[0])[2:].zfill(8)
            project_sn = f"16#{sn_raw[:4].upper()}_{sn_raw[4:].upper()}"
        else:
            project_sn = "Unknown"

        raw_modified_date = struct.unpack("<Q", extended_records[0x66])[0] / 10000000
        last_modified_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_modified_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        raw_created_date = struct.unpack("<Q", extended_records[0x65])[0] / 10000000
        project_creation_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_created_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        # MajorRev and MinorRev from ext[0x076] bytes[3] and [2]
        rev_bytes = extended_records.get(0x076, b"\x00\x00\x00\x00")
        major_rev = str(rev_bytes[3]) if len(rev_bytes) >= 4 else "0"
        minor_rev = str(rev_bytes[2]) if len(rev_bytes) >= 3 else "0"

        # MajorFaultProgram from ext[0x068] OID → comp_name lookup
        major_fault_program: Union[str, None] = None
        if 0x068 in extended_records and len(extended_records[0x068]) >= 4:
            mfp_oid = struct.unpack_from("<I", extended_records[0x068])[0]
            if mfp_oid and mfp_oid != 0xFFFFFFFF:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(mfp_oid))
                mfp_row = self._cur.fetchone()
                major_fault_program = mfp_row[0] if mfp_row else None

        self._object_id = results[0][1]
        controller_name = results[0][0]

        # Get the data types
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxDataTypeCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller data type collection")

        _data_type_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_data_type_id)
        )
        results = self._cur.fetchall()

        data_types: List[DataType] = []
        for result in results:
            _data_type_object_id = result[1]
            dt = DataTypeBuilder(self._cur, _data_type_object_id).build()
            if dt.cls == "User":
                data_types.append(dt)

        # Get the Controller Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller tag collection")
        _tag_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_tag_collection_object_id)
        )
        results = self._cur.fetchall()
        tags: List[Tag] = []
        for result in results:
            _tag_object_id = result[1]
            tag = TagBuilder(self._cur, _tag_object_id).build()
            if tag.data_type and not tag.name.startswith("$") and ":" not in tag.name and not tag.name.startswith("__"):
                tags.append(tag)

        # Get the Program Collection and get the programs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxProgramCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller program collection")

        _program_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_program_collection_object_id)
        )
        results = self._cur.fetchall()
        programs: List[Program] = []
        for result in results:
            _program_object_id = result[1]
            programs.append(ProgramBuilder(self._cur, _program_object_id).build())

        # Build comment_id → program name map for task scheduled-program resolution.
        # comment_id is a u16 at BLOB offset 0x0C in each program's RxGeneric record.
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE parent_id=" + str(_program_collection_object_id)
        )
        comment_id_to_program: Dict[int, str] = {
            struct.unpack_from("<H", rec, 0x0C)[0]: pname
            for pname, rec in self._cur.fetchall()
        }

        # Get the Task Collection and build Tasks
        self._cur.execute(
            "SELECT comp_name, object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTaskCollection'"
        )
        task_coll_results = self._cur.fetchall()
        tasks: List[Task] = []
        if task_coll_results:
            _task_collection_object_id = task_coll_results[0][1]
            self._cur.execute(
                "SELECT comp_name, object_id FROM comps WHERE parent_id="
                + str(_task_collection_object_id)
                + " AND record_type=256"
            )
            for task_result in self._cur.fetchall():
                tasks.append(TaskBuilder(self._cur, task_result[1]).build(comment_id_to_program))

        # Get the AOI Collection and get the AOIs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxUDIDefinitionCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one AOI collection")
        _aoi_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_aoi_collection_object_id)
        )
        results = self._cur.fetchall()
        aois: List[AOI] = []
        for result in results:
            _aoi_object_id = result[1]
            aois.append(AoiBuilder(self._cur, _aoi_object_id).build())

        # Get the Module (IO) Collection and build all Module elements.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxMapDeviceCollection'"
        )
        row = self._cur.fetchone()
        if row is None:
            modules: List[Module] = []
        else:
            coll_oid = row[0]
            self._cur.execute(
                "SELECT comp_name, object_id, record FROM comps WHERE parent_id="
                + str(coll_oid)
                + " ORDER BY seq_number"
            )
            mod_rows = self._cur.fetchall()

            # First pass: build modid→name map so child modules can resolve their parent name.
            from acd.generated.comps.rx_generic import RxGeneric as _RxG
            modid_to_name: Dict[int, str] = {}
            for db_name, mod_oid, mod_rec in mod_rows:
                display_name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name
                try:
                    r = _RxG.from_bytes(bytes(mod_rec))
                    if r.cip_type == 0x69:
                        exts = {er.attribute_id: bytes(er.value) for er in r.extended_records}
                        e1 = exts.get(0x001, b"")
                        if len(e1) >= 0x30:
                            modid = struct.unpack("<I", e1[0x2C:0x30])[0]
                            modid_to_name[modid] = display_name
                except Exception:
                    pass

            # Second pass: build Module objects.
            modules = []
            for _, mod_oid, _ in mod_rows:
                modules.append(
                    ModuleBuilder(self._cur, mod_oid, modid_to_name).build()
                )

            # Third pass: compute (parent_name, parent_port_id) → child count,
            # then inject the relevant sub-dict into each Module as _port_child_counts.
            child_counts: Dict[tuple, int] = {}
            for m in modules:
                k = (m.parent_module, m.parent_mod_port_id)
                child_counts[k] = child_counts.get(k, 0) + 1
            for m in modules:
                m._port_child_counts = {
                    port_id: child_counts.get((m.name, port_id), 0)
                    for port_id in range(1, 20)
                    if (m.name, port_id) in child_counts
                }

        # ProcessorType is the CatalogNumber of the root controller module (the one
        # whose parent is itself, i.e. MajorFault="true").
        processor_type = next(
            (m.catalog_number for m in modules if m.major_fault == "true" and m.catalog_number),
            None,
        )

        return Controller(
            controller_name,
            "Target",
            controller_name,
            processor_type,
            major_rev,
            minor_rev,
            major_fault_program,
            project_creation_date,
            last_modified_date,
            sfc_execution_control,
            sfc_restart_position,
            sfc_last_scan,
            project_sn,
            "false",        # MatchProjectToController
            "false",        # CanUseRPIFromProducer
            "0",            # InhibitAutomaticFirmwareUpdate
            "EnabledWithAppend",  # PassThroughConfiguration
            "true",         # DownloadProjectDocumentationAndExtendedProperties
            "true",         # DownloadProjectCustomProperties
            "false",        # ReportMinorOverflow
            "false",        # AutoDiagsEnabled
            "false",        # WebServerEnabled
            data_types,
            modules,
            tags,
            programs,
            tasks,
            aois,
        )


@dataclass
class ProjectBuilder:
    quick_info_filename: PathLike

    def build(self) -> RSLogix5000Content:
        element = ET.parse(self.quick_info_filename)
        rslogix_content_element = element.find(".")
        if rslogix_content_element is not None:
            target_name = rslogix_content_element.attrib["Name"]

        schema_version_element = element.find("SchemaVersion")
        if schema_version_element is not None:
            schema_version_major = schema_version_element.attrib["Major"]
            schema_version_minor = schema_version_element.attrib["Minor"]
            schema_revision = f"{schema_version_major}.{schema_version_minor}"
        else:
            schema_revision = "1.0"

        software_revision_element = element.find("DeviceIdentity")
        if software_revision_element is not None:
            software_revision_major = software_revision_element.attrib["MajorRevision"]
            software_revision_minor = software_revision_element.attrib["MinorRevision"]
            software_revision = f"{software_revision_major}.{software_revision_minor}"
        else:
            software_revision = "33.01"

        target_type = "Controller"
        contains_context = "false"
        now = datetime.now()
        export_date = now.strftime("%a %b %d %H:%M:%S %Y")
        export_options = (
            "NoRawData L5KData DecoratedData ForceProtectedEncoding AllProjDocTrans"
        )
        return RSLogix5000Content(
            target_name,
            None,
            schema_revision,
            software_revision,
            target_name,
            target_type,
            contains_context,
            export_date,
            export_options,
        )


@dataclass
class DumpCompsRecords(L5xElementBuilder):
    base_directory: PathLike = Path("dump")

    def dump(self, parent_id: int = 0, log_file=None):
        self._cur.execute(
            f"SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id={parent_id}"
        )
        results = self._cur.fetchall()

        for result in results:
            object_id = result[1]
            name = result[0]
            record = result[4]
            new_path = Path(os.path.join(self.base_directory, name))
            if os.path.exists(os.path.join(new_path)):
                shutil.rmtree(os.path.join(new_path))
            if not os.path.exists(os.path.join(new_path)):
                os.makedirs(new_path)
            with open(Path(os.path.join(new_path, name + ".dat")), "wb") as file:
                log_file.write(
                    f"Class - {struct.unpack_from('<H', result[4], 0xA)[0]} Instance {struct.unpack_from('<H', result[4], 0xC)[0]}- {str(new_path) + '/' + name}\n"
                )
                file.write(record)

            DumpCompsRecords(self._cur, object_id, new_path).dump(object_id, log_file)
