from __future__ import annotations

from app import models
from app.services.nvr import NVRClient, parse_xml


def sample_nvr() -> models.NVR:
    return models.NVR(
        name="Test NVR",
        code="test_nvr",
        campus="tk",
        zone="ecp",
        protocol="hikvision",
        host="192.0.2.10",
        port=80,
        username="admin",
        password_enc="",
        channel_offset=32,
    )


def test_parse_xml_strips_namespaces():
    xml = (
        '<?xml version="1.0"?>'
        '<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        "<deviceName>NVR-1</deviceName><model>DS-7608</model>"
        "</DeviceInfo>"
    )
    root = parse_xml(xml)
    assert root is not None
    assert root.findtext("deviceName") == "NVR-1"
    assert root.findtext("model") == "DS-7608"


def test_parse_xml_malformed_prefix_is_repaired():
    xml = "<ns:DeviceInfo><ns:model>DS-2CD</ns:model></ns:DeviceInfo>"
    root = parse_xml(xml)
    assert root is not None
    assert root.findtext("model") == "DS-2CD"


def test_parse_xml_invalid_returns_none():
    assert parse_xml("") is None
    assert parse_xml("not xml at all") is None


def test_discover_cameras_and_status(monkeypatch):
    client = NVRClient(sample_nvr())

    channels = """<?xml version="1.0"?>
    <InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <InputProxyChannel>
        <id>1</id><name>Gate</name>
      </InputProxyChannel>
      <InputProxyChannel>
        <id>2</id><name>Lobby</name>
      </InputProxyChannel>
    </InputProxyChannelList>"""

    status = """<?xml version="1.0"?>
    <InputProxyChannelStatusList>
      <InputProxyChannelStatus><id>1</id><online>true</online></InputProxyChannelStatus>
      <InputProxyChannelStatus><id>2</id><online>false</online></InputProxyChannelStatus>
    </InputProxyChannelStatusList>"""

    def fake_get_text(path: str) -> str:
        if path.endswith("/channels"):
            return channels
        if "channels/status" in path:
            return status
        return ""

    monkeypatch.setattr(client, "get_text", fake_get_text)
    cameras = client.discover_cameras()
    assert [camera["channel"] for camera in cameras] == [1, 2]
    assert cameras[0]["name"] == "Gate"
    assert cameras[0]["online"] is True
    assert cameras[1]["online"] is False


def test_discover_cameras_reads_nested_ip(monkeypatch):
    client = NVRClient(sample_nvr())
    channels = """<?xml version="1.0"?>
    <InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <InputProxyChannel>
        <id>1</id><name>Gate</name>
        <sourceInputPortDescriptor>
          <proxyProtocol>HIKVISION</proxyProtocol>
          <ipAddress>172.20.16.6</ipAddress>
          <managePortNo>8000</managePortNo>
        </sourceInputPortDescriptor>
      </InputProxyChannel>
    </InputProxyChannelList>"""
    monkeypatch.setattr(client, "get_text", lambda path: channels if path.endswith("/channels") else "")
    cameras = client.discover_cameras()
    assert cameras[0]["ip_address"] == "172.20.16.6"
    assert cameras[0]["mac_address"] is None


def test_fill_missing_macs_and_ip_change(monkeypatch):
    from types import SimpleNamespace

    from app.services import nvr as nvr_service

    client = NVRClient(sample_nvr())
    monkeypatch.setattr(nvr_service, "_mac_failures", {})
    lookups = {"192.0.2.21": "FC-9F-FD-A1-32-7E", "192.0.2.22": None}
    monkeypatch.setattr(client, "camera_mac", lambda ip, timeout=4.0: nvr_service.normalise_mac(lookups[ip]))
    cams = [
        SimpleNamespace(ip_address="192.0.2.21", mac_address=None),
        SimpleNamespace(ip_address="192.0.2.22", mac_address=None),
        SimpleNamespace(ip_address=None, mac_address=None),
    ]
    assert client.fill_missing_macs(cams) == 1
    assert cams[0].mac_address == "fc:9f:fd:a1:32:7e"
    assert cams[1].mac_address is None
    # A failed camera is not retried on the next sweep unless forced.
    lookups["192.0.2.22"] = "08:cc:81:97:1e:90"
    assert client.fill_missing_macs(cams) == 0
    assert client.fill_missing_macs(cams, force=True) == 1

    camera = SimpleNamespace(ip_address="192.0.2.21", mac_address="fc:9f:fd:a1:32:7e")
    nvr_service.apply_camera_address(camera, {"ip_address": "192.0.2.99"})
    assert camera.ip_address == "192.0.2.99" and camera.mac_address is None


def test_discover_dvr_analog_channel_signal(monkeypatch):
    client = NVRClient(sample_nvr())
    channels = """<?xml version="1.0"?>
    <VideoInputChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <VideoInputChannel>
        <id>1</id><name>Gate</name>
        <videoInputEnabled>true</videoInputEnabled><resDesc>  1080P25</resDesc>
      </VideoInputChannel>
      <VideoInputChannel>
        <id>2</id><name>Lobby</name>
        <videoInputEnabled>true</videoInputEnabled><resDesc>NO VIDEO</resDesc>
      </VideoInputChannel>
      <VideoInputChannel>
        <id>3</id><name>Spare</name>
        <videoInputEnabled>false</videoInputEnabled><resDesc>1080P</resDesc>
      </VideoInputChannel>
    </VideoInputChannelList>"""

    def fake_get_text(path: str) -> str:
        if "System/Video/inputs/channels" in path:
            return channels
        return ""

    monkeypatch.setattr(client, "get_text", fake_get_text)
    cameras = client.discover_cameras()
    assert [camera["channel"] for camera in cameras] == [1, 2, 3]
    assert cameras[0]["online"] is True
    assert cameras[1]["online"] is False
    assert cameras[2]["online"] is False


def test_working_status_reports_dvr_channel_signal(monkeypatch):
    client = NVRClient(sample_nvr())
    payload = """{"ChanStatus":[
      {"chanNo":1,"enable":1,"online":1,"signal":0},
      {"chanNo":2,"enable":1,"online":1,"signal":1},
      {"chanNo":3,"enable":0,"online":1,"signal":0},
      {"chanNo":4,"enable":1,"online":0,"signal":0}
    ]}"""

    def fake_get_text(path: str) -> str:
        if "workingstatus/chanStatus" in path:
            return payload
        return ""

    monkeypatch.setattr(client, "get_text", fake_get_text)
    status = client._channel_status()
    assert status[1] is True
    assert status[2] is False  # signal loss
    assert status[3] is False  # disabled analog channel
    assert status[4] is False  # offline


def test_storage_json_variant(monkeypatch):
    client = NVRClient(sample_nvr())
    payload = """{"hddList":[{"id":"1","name":"HDD1","capacity":1000,"freeSpace":250,"status":"ok"}]}"""

    monkeypatch.setattr(client, "get_text", lambda path: payload if "Storage" in path else "")
    disks = client.get_storage()
    assert len(disks) == 1
    assert disks[0]["used_percent"] == 75.0
    assert disks[0]["name"] == "HDD1"
