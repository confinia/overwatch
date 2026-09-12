# NORBI (46494) TMI-0 telemetry, LoRa downlink. Written by Gustavo, LW2DTZ,
# from the translated NORBY specification; CC0-1.0. Pending upstream as
# https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders/-/merge_requests/524
# (ksy/norbi.ksy at commit 2cc76e8b). Vendored unchanged below; delete once a
# satnogs-decoders release ships it (#458).
#
# This is a generated file! Please edit source .ksy file and use kaitai-struct-compiler to rebuild
# Generated for satnogs-decoders using kaitai-struct-compiler 0.10
# Source: ksy/norbi.ksy

import kaitaistruct
from kaitaistruct import KaitaiStruct, KaitaiStream, BytesIO


if getattr(kaitaistruct, 'API_VERSION', (0, 9)) < (0, 9):
    raise Exception("Incompatible Kaitai Struct Python API: 0.9 or later is required, but you have %s" % (kaitaistruct.__version__))


class Norbi(KaitaiStruct):
    """NORBI (also known as NORBY) is a 6U CubeSat developed by Siberian State
    University (SibSU), Russia. Launched September 28, 2020 on Soyuz-2-1b.
    Transmits LoRa telemetry on 436.525 MHz.
    NORAD ID: 46494

    References:
      - https://www.qsl.net/k/k4kdr//files/norby-specs-translated-english.pdf
      - https://github.com/4m1g0/tinygs-decoders/blob/master/ksy/norbi.ksy

    .. seealso::
       Source - https://db.satnogs.org/satellite/46494


    .. parsed-literal::

        :field frame_number: payload.frame_number
        :field frame_generation_time: payload.frame_generation_time
        :field brk_title: payload.brk_title
        :field brk_number_active: payload.brk_number_active
        :field brk_restarts_count_active: payload.brk_restarts_count_active
        :field brk_current_mode_id: payload.brk_current_mode_id
        :field brk_transmitter_power_active: payload.brk_transmitter_power_active
        :field brk_temp_active: payload.brk_temp_active
        :field brk_voltage_offset_amplifier_active: payload.brk_voltage_offset_amplifier_active
        :field brk_last_received_packet_rssi_active: payload.brk_last_received_packet_rssi_active
        :field brk_last_received_packet_snr_active: payload.brk_last_received_packet_snr_active
        :field ms_temp: payload.ms_temp
        :field sop_altitude_glonass: payload.sop_altitude_glonass
        :field sop_latitude_glonass: payload.sop_latitude_glonass
        :field sop_longitude_glonass: payload.sop_longitude_glonass
        :field sop_magnetic_induction_module: payload.sop_magnetic_induction_module
        :field sop_mk_temp_dsg1: payload.sop_mk_temp_dsg1
        :field sop_mk_temp_dsg6: payload.sop_mk_temp_dsg6
        :field sop_board_temp: payload.sop_board_temp
        :field ses_median_panel_x_temp_positive: payload.ses_median_panel_x_temp_positive
        :field ses_median_panel_x_temp_negative: payload.ses_median_panel_x_temp_negative
        :field ses_charge_level_m_ah: payload.ses_charge_level_m_ah
        :field ses_total_charging_power: payload.ses_total_charging_power
        :field ses_total_generated_power: payload.ses_total_generated_power
        :field ses_total_power_load: payload.ses_total_power_load
        :field ses_median_pmm_temp: payload.ses_median_pmm_temp
        :field ses_median_pam_temp: payload.ses_median_pam_temp
        :field ses_median_pdm_temp: payload.ses_median_pdm_temp
        :field ses_voltage: payload.ses_voltage
    """

    def __init__(self, _io, _parent=None, _root=None):
        self._io = _io
        self._parent = _parent
        self._root = _root if _root else self
        self._read()

    def _read(self):
        self.header = Norbi.Header(self._io, self, self._root)
        if self.header.msg_type_id == 0:
            self._raw_payload = self._io.read_bytes((self.header.length - 14))
            _io__raw_payload = KaitaiStream(BytesIO(self._raw_payload))
            self.payload = Norbi.Tmi0(_io__raw_payload, self, self._root)


    class Header(KaitaiStruct):
        """LoRa packet header (14 bytes). msg_type_id == 0 means TMI-0 telemetry frame."""

        def __init__(self, _io, _parent=None, _root=None):
            self._io = _io
            self._parent = _parent
            self._root = _root if _root else self
            self._read()

        def _read(self):
            self.length = self._io.read_u1()
            self.receiver_address = self._io.read_u4le()
            self.transmitter_address = self._io.read_u4le()
            self.transaction_number = self._io.read_u2le()
            self.reserved = self._io.read_bytes(2)
            self.msg_type_id = self._io.read_s2be()


    class Tmi0(KaitaiStruct):
        """TMI-0 telemetry frame. Contains BRK (radio), MS (mission system),
        SOP (orientation/GNSS) and SES (power/energy) subsystem data.
        """

        def __init__(self, _io, _parent=None, _root=None):
            self._io = _io
            self._parent = _parent
            self._root = _root if _root else self
            self._read()

        def _read(self):
            self.frame_start_mark = self._io.read_bytes(2)
            if not (self.frame_start_mark == b"\xF1\x0F"):
                raise kaitaistruct.ValidationNotEqualError(b"\xF1\x0F", self.frame_start_mark, self._io, u"/types/tmi0/seq/0")
            self.frame_definition = self._io.read_u2le()
            self.frame_number = self._io.read_u2le()
            self.frame_generation_time = self._io.read_u4le()
            # BRK — radio/comms subsystem
            self.brk_title = (self._io.read_bytes(24)).decode(u"ASCII")
            self.brk_number_active = self._io.read_u1()
            self.brk_restarts_count_active = self._io.read_s4le()
            self.brk_current_mode_id = self._io.read_u1()
            self.brk_transmitter_power_active = self._io.read_s1()
            self.brk_temp_active = self._io.read_s1()
            self.brk_module_state_active = self._io.read_bytes(2)
            self.brk_voltage_offset_amplifier_active = self._io.read_u2le()
            self.brk_last_received_packet_rssi_active = self._io.read_s1()
            self.brk_last_received_packet_snr_active = self._io.read_s1()
            self.brk_archive_record_pointer = self._io.read_u2le()
            self.brk_last_received_packet_snr_inactive = self._io.read_s1()
            # MS — mission/main system
            self.ms_module_state = self._io.read_bytes(2)
            self.ms_payload_state = self._io.read_bytes(2)
            self.ms_temp = self._io.read_s1()
            self.ms_pn_supply_state = self._io.read_u1()
            # SOP — orientation and GNSS subsystem
            self.sop_altitude_glonass = self._io.read_s4le()
            self.sop_latitude_glonass = self._io.read_s4le()
            self.sop_longitude_glonass = self._io.read_s4le()
            self.sop_date_time_glonass = self._io.read_u4le()
            self.sop_magnetic_induction_module = self._io.read_u2le()
            self.sop_angular_velocity_vector = self._io.read_bytes(6)
            self.sop_angle_priority1 = self._io.read_u2le()
            self.sop_angle_priority2 = self._io.read_u2le()
            self.sop_mk_temp_dsg1 = self._io.read_s1()
            self.sop_mk_temp_dsg6 = self._io.read_s1()
            self.sop_board_temp = self._io.read_s1()
            self.sop_state = self._io.read_bytes(2)
            self.sop_state_dsg = self._io.read_bytes(6)
            self.sop_orientation_number = self._io.read_u1()
            # SES — power/energy subsystem
            self.ses_median_panel_x_temp_positive = self._io.read_s1()
            self.ses_median_panel_x_temp_negative = self._io.read_s1()
            self.ses_solar_panels_state = self._io.read_bytes(5)
            self.ses_charge_level_m_ah = self._io.read_u2le()
            self.ses_battery_state = self._io.read_bytes(3)
            self.ses_charging_keys_state = self._io.read_bytes(2)
            self.ses_power_line_state = self._io.read_u1()
            self.ses_total_charging_power = self._io.read_s2le()
            self.ses_total_generated_power = self._io.read_u2le()
            self.ses_total_power_load = self._io.read_u2le()
            self.ses_median_pmm_temp = self._io.read_s1()
            self.ses_median_pam_temp = self._io.read_s1()
            self.ses_median_pdm_temp = self._io.read_s1()
            self.ses_module_state = self._io.read_bytes(3)
            self.ses_voltage = self._io.read_u2le()
            self.crc16 = self._io.read_u2le()
