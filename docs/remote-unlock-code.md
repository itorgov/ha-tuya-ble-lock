# Find an existing DP61 remote unlock code

This procedure reads a command sent by your own Tuya app to your own lock. It does not program a code or send a command from Home Assistant. You need a linked Tuya cloud project and one successful app unlock performed through the gateway.

## Query the relevant command

1. In the [Tuya Developer Platform](https://platform.tuya.com/), open **Cloud → API Explorer** and select the linked project and correct data center. The alternative entry point is **project → Service API → Debug**. [API Explorer documentation](https://developer.tuya.com/en/docs/im-project-services/Cloud-self-development?id=Kb3o4z4ll4v9d).
2. Select **Query device logs**, the read-only `GET /v1.0/devices/{device_id}/logs` operation. Use the **lock Device ID**, not the gateway ID.
3. Set `type` to `5,7`, `start_time` and `end_time` to Unix timestamps in **milliseconds** covering a few minutes around the successful app unlock, and `size` to `100`. If available, use `codes=remote_no_dp_key` and the free-edition `query_type=1`. You can omit the code filter if it hides the command. Query/paginate only that small interval. [Tuya device-log API](https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a).
4. Locate the outgoing DP61 / `remote_no_dp_key` entry corresponding to that successful unlock. Copy its **raw value**, normally a base64 string. It must decode to 13 bytes in the layout below. A three-byte acknowledgement is not the command.

Some accounts offer the same information through the device's **Debug Device → Device Logs** view. Portal labels and permissions vary. If the API is not authorized, inspect the project's Service API subscriptions and authorization. A status-only log is insufficient.

```text
Byte offset    Length    Meaning
0              1         0x01
1              2         Member number, unsigned big-endian
3              8         Existing remote unlock code, ASCII digits
11             2         0x00 0x00
```

This layout comes from the supported protocol and [a direct investigation of DP61 gateway commands](https://github.com/make-all/tuya-local/issues/6248). It is not a claim that every Tuya lock uses this format. Values reported by DP69/DP71/DP73 may identify a lock user instead of containing the unlock code.

## Import into Home Assistant

1. During the integration's lock setup, confirm the supported DP61 profile.
2. Paste only the base64 value into **Existing DP61 command (Base64)**, without JSON quotes or spaces. Leave the separate remote-code field blank. The payload's member number takes precedence over the member field.
3. Submit the form. The integration checks the layout and stores the extracted member and code. Parsing happens locally; it does not send the payload to Tuya or attempt an unlock.

If the portal presents hexadecimal instead, do not paste it into the base64 field. Obtain the raw base64 representation or decode the supported layout locally and enter the resulting member and eight-digit code directly. Preserve leading zeroes and avoid online decoder websites: the payload contains a working credential.

A later app action can replace the code on some models, so a previously valid value may stop working. If the raw command is unavailable or has a different layout, report the model and redacted schema rather than guessing a code.
