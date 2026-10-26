"""Shared MQTT 5 helpers: JSON cloud and legacy PB business registration for the local-only simulators."""
from pathlib import Path
import sys
from google.protobuf.json_format import MessageToJson, Parse
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build/python"))
import cloud_pb2 as cloud
import business_pb2 as business
import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes


def properties(json_wire=False):
    p = Properties(PacketTypes.PUBLISH)
    p.UserProperty = [("proto", "1")] + ([("ct", "json")] if json_wire else [])
    if json_wire:
        p.ContentType = "application/json"
    return p


def publish(client, topic, message, retain=False):
    json_wire = not topic.startswith("local/")
    payload = MessageToJson(message) if json_wire else message.SerializeToString()
    return client.publish(topic, payload, qos=1,
                          retain=retain, properties=properties(json_wire))


def parse_cloud(payload, message):
    return Parse(payload.decode("utf-8"), message, ignore_unknown_fields=True)


def wait_for(predicate, timeout=15, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise TimeoutError(f"timed out waiting for {description}")


class BusinessSimulator:
    def __init__(self, port, device, registration, host="127.0.0.1"):
        self.registration = registration
        self.base = f"local/v1/{device}/biz/{registration.business_id}"
        self.connected = threading.Event()
        self.acks = []
        self.client = mqtt.Client(client_id=f"biz-{registration.business_id}", protocol=mqtt.MQTTv5)

        def connected(client, userdata, flags, reason, props):
            if reason == 0:
                client.subscribe(self.base + "/down/register_ack", qos=1)

        def subscribed(client, userdata, mid, codes, props):
            publish(client, self.base + "/up/register", self.registration, retain=True)
            self.connected.set()

        def message(client, userdata, msg):
            ack = business.RegistrationAck()
            ack.ParseFromString(msg.payload)
            self.acks.append(ack)

        self.client.on_connect = connected
        self.client.on_subscribe = subscribed
        self.client.on_message = message
        self.client.connect(host, port, 30)
        self.client.loop_start()
        if not self.connected.wait(5):
            raise TimeoutError("business MQTT subscription")

    def register(self, registration):
        self.registration = registration
        before = len(self.acks)
        publish(self.client, self.base + "/up/register", registration, retain=True)
        return wait_for(lambda: self.acks[before:] and self.acks[-1], description="registration ACK")

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()
