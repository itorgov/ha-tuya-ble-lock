# Tuya BLE Lock for Home Assistant

Control compatible Tuya Bluetooth locks from Home Assistant through their existing Wi-Fi/Bluetooth gateway. Each configured lock appears as a separate device with a `lock` entity, ready for dashboards, scripts, and automations such as a wireless button that unlocks a door.

Requires **Home Assistant 2026.10 or newer**.

```text
Home Assistant ── local network ── Tuya gateway ── Bluetooth ── lock
```

Tuya Cloud helps retrieve gateway credentials and discover its child devices during setup. Its API credentials are not saved. Unlocking and availability checks then use the local network. Home Assistant does not need a Bluetooth adapter. Manual setup is available if you already have the required device details.

## Compatibility

This is a custom integration for the **HX201-style DP61 remote-unlock protocol**, not a universal driver for every Tuya lock. The underlying command was verified on an HX201-family lock through a gateway using LAN protocol 3.5. Protocols 3.3 and 3.4 are accepted but require hardware verification on your gateway.

Your lock must support the raw `remote_no_dp_key` / DP61 command and have an existing eight-digit remote unlock code. Tuya branding, Bluetooth support, or discovery alone does not establish compatibility. The integration does not create remote codes, enroll users, pair devices, or program fingerprints/cards.

- **Supported:** unlock through `lock.unlock`, separate devices for multiple locks, local availability checks, UI configuration.
- **Not supported:** locking, unlatching as a separate action, key registration, or reliable locked/unlocked feedback.
- **State:** a reachable lock can show **Unknown**. A command acknowledgement does not prove physical movement. The integration does not invent a locked state or an automatic relock timer.
- **No automatic unlock retry:** if a command times out, it might already have opened the lock. Check the door before trying again.

An optional battery sensor reads the supported profile's DP8 percentage. It is disabled by default; enable it in the lock device's entity list if your model reports that value. It remains unknown when the gateway cannot provide a fresh reading.

The gateway must be reachable at a private IPv4 address from Home Assistant, with TCP port **6668** allowed. Reserve its IP address in your router. Do not forward this port to the internet. Keep a working physical means of entry while testing.

## 1. Set up the gateway and locks in the mobile app

1. Install **Tuya Smart** or **Smart Life**, as specified by the device manufacturer. Register a permanent account, sign in, and create a home if requested. Use the same account throughout setup.
2. Enable Bluetooth and the app's requested nearby-device permissions. Connect your phone to the gateway's supported Wi-Fi network, normally **2.4 GHz**.
3. Power the gateway and put it in pairing mode following its manual. In the app, choose **+ → Add Device** and add the Bluetooth gateway; the manual category is usually **Gateway Control → Wireless Gateway (Bluetooth)**. Enter your Wi-Fi credentials and finish pairing. [Tuya's gateway instructions](https://support.tuya.com/en/help/_detail/K9hut3ffwx4az).
4. Put each lock in pairing mode and add it **inside the app**, not in the phone's Bluetooth settings. Finish any administrator/owner setup required by the lock. [Tuya's lock instructions](https://support.tuya.com/en/help/_detail/K9hw4ebtzv7rb).
5. Open the gateway's device page and add/bind each lock through **Add sub-device** or **Add device by list**, depending on the gateway panel. Verify that each lock is listed under that gateway. Follow the product manual if its binding sequence differs. [Tuya's sub-device workflow](https://developer.tuya.com/en/docs/iot-device-dev/tuyaos-gateway-join?id=Kc3234gqbhjjt).
6. Verify that the app can unlock the lock through the gateway, with your phone's Bluetooth disabled. This is a real unlock: test while you are beside the door. If this fails, resolve pairing, range, batteries, and the lock's remote-unlock settings before continuing.

## 2. Create and authorize a Tuya cloud project

The developer account is separate from the mobile-app account. Linking the app in the next step gives the project access to your devices.

1. Register/sign in at the [Tuya Developer Platform](https://platform.tuya.com/).
2. Open **Cloud → Development** (also shown as **Project Management**) and select **Create Cloud Project**.
3. Enter a name, choose **Smart Home** as the development method, and select the data center serving your **app account's registered region**. Check that region in the app under **Me → Settings → Account and Security → Region**. Use [Tuya's region/data-center mapping](https://developer.tuya.com/en/docs/iot/oem-app-data-center-distributed?id=Kafi0ku9l07qb), including its notes for older accounts; do not infer the data center from your current location.
4. Accept the Smart Home API services offered by the wizard. Tuya currently lists **Industry Basic Service**, **Smart Home Basic Service**, and **Device Status Notification**. Copy the project's **Access ID / Client ID** and **Access Secret / Client Secret** from **Overview** into a password manager. [Official project wizard](https://developer.tuya.com/en/docs/iot/Platform_Configuration_smarthome?id=Kamcgamwoevrx).
5. Under **Service API**, ensure the project is authorized for **IoT Core** and **Authorization** / **Authorization Token Management**, plus the device-management services above. If missing, use **Go to Authorize**. Subscribing to a service and authorizing this project are separate steps. You can inspect available services under **Cloud → Cloud Services → My Service**. [API authorization instructions](https://developer.tuya.com/en/docs/iot/applying-for-api-group-permissions?id=Ka6vf012u6q76).

Tuya can change service names, eligibility, quotas, and trial terms. Check the subscriptions shown for your account; a developer project does not guarantee permanent free API access. Expired cloud access affects discovery and credential retrieval, not already configured local commands while the local credentials remain valid.

## 3. Link the mobile-app account and collect the gateway details

1. In your project, open **Devices → Link Tuya App Account → Add App Account**. Use this option for Tuya Smart/Smart Life, not **Link My App**, which is for apps you develop.
2. Scan the displayed QR code with the scanner **inside the same mobile app/account** used in step 1. Confirm the authorization on the phone.
3. Select automatic device linking, or explicitly include the gateway and all intended locks if using manual linking.
4. Open **All Devices**, select the matching data center, and confirm that both the gateway and locks appear. If the list is empty, check the app account, data center, and linking permissions. [Official account-linking instructions](https://developer.tuya.com/en/docs/iot/link-devices?id=Ka471nu1sfmkl).
5. Copy the **gateway's Device ID**. This is different from each lock's Device ID. Find the gateway's local IP address in your router's client/DHCP list and reserve it.
6. Determine its LAN protocol version. A read-only [TinyTuya network scan](https://github.com/jasonacox/tinytuya#network-scanner), run on a computer on the same LAN, reports the gateway ID, IP, and version:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install tinytuya==1.20.0
   python -m tinytuya scan
   ```

   On Windows, activate with `.venv\Scripts\activate`. Broadcast discovery needs UDP 6666, 6667, and 7000 on that LAN. The integration itself connects to the configured IP; a broadcast scan is not required at runtime. Use the observed version, not a guess.

## 4. Obtain each lock's remote unlock code

Three credentials have different purposes:

| Value | Purpose | Where it comes from |
| --- | --- | --- |
| Project Access ID and Secret | Read Tuya Cloud during discovery | Cloud project → Overview |
| Gateway local key | Encrypt the connection to the gateway | Retrieved during cloud setup; manual alternative below |
| Lock remote unlock code and member number | Authorize the DP61 unlock command | Existing lock configuration or a successful app command log |

The remote unlock code is exactly **eight digits**, including leading zeroes. It is not the gateway local key, your app password, a lock user ID, or necessarily the PIN typed on the lock's keypad. Do not substitute an arbitrary eight-digit value.

If the manufacturer's app exposes the existing DP61 remote code and member number, use them. Otherwise, inspect **your own successful gateway-based app unlock** in the Tuya device command logs:

1. Note the time of the successful app unlock from step 1.
2. Open **Cloud → API Explorer**, select your project, and locate **Query device logs** for the **lock Device ID**. Query a narrow time range around that action, with log types `5,7`; if offered, filter the code to `remote_no_dp_key`.
3. Find the **outgoing command** for `remote_no_dp_key` / DP61, not its short response. Copy its raw base64 value privately. The supported layout is `01 | member (2 bytes, big-endian) | code (8 ASCII digits) | 00 00`.
4. During lock configuration, paste it into **Existing DP61 command (Base64)**; the integration extracts the member and code locally. Alternatively, enter the code and member directly. See the [detailed log-query instructions](docs/remote-unlock-code.md) if needed.

Some products hide the command value, use a different format, or rotate the code when the app unlocks. If you cannot obtain a currently registered compatible code, this integration cannot unlock that model. It deliberately does not register or replace a code. [Protocol evidence](https://github.com/make-all/tuya-local/issues/6248).

## 5. Install the integration

### HACS custom repository

1. [Install and configure HACS](https://www.hacs.dev/docs/use/download/download/) if needed.
2. In **HACS → ⋮ → Custom repositories**, add `https://github.com/itorgov/ha-tuya-ble-lock` with type **Integration**.
3. Find **Tuya BLE Lock**, download it, and restart Home Assistant. See [HACS custom repository instructions](https://www.hacs.dev/docs/faq/custom_repositories/).

### Manual installation

1. Download this repository and copy `custom_components/tuya_ble_lock` into your Home Assistant configuration directory:

   ```text
   config/
     custom_components/
       tuya_ble_lock/
         __init__.py
         manifest.json
         ...
   ```

2. Restart Home Assistant. Dependencies are installed automatically; do not install TinyTuya manually inside Home Assistant.

## 6. Add the gateway and its locks

1. Open **Settings → Devices & services → Add integration** and search for **Tuya BLE Lock**.
2. Choose **Discover locks with Tuya Cloud**. Enter the project's Access ID, Secret, data center, and the **gateway Device ID**. This retrieves the gateway local key and its child devices.
3. Enter the gateway's **local IP** and verified **LAN protocol version**. Leave the local-key field blank to retain the imported key.
4. Select the compatible locks you want to add. For each lock, confirm the supported DP61 profile and enter its remote unlock code and member, or paste the existing DP61 command from step 4. Discovery cannot verify mechanical behavior or code validity without unlocking.
5. Select **Save** in the lock-management menu. Each configured lock has its own device and `lock` entity, linked to the gateway. You can leave a code blank to complete discovery, but unlocking remains unavailable until you supply it. Repeat for another gateway.

Setup only reads cloud/device information; it does not unlock anything. Perform your first explicit unlock from **Developer tools → Actions → Lock: Unlock** while beside the door. Select the new entity and run the action once.

### Manual setup

Choose manual setup if you already have the gateway local key and the lock identifiers. You need:

| Field | Required value |
| --- | --- |
| Gateway host | Its private IPv4 address on your LAN |
| Gateway Device ID | The gateway's Tuya Device ID |
| Gateway local key | Its exact 16-byte key, preserving punctuation |
| Protocol version | The gateway's observed 3.3, 3.4, or 3.5 version |
| Lock Device ID | The child's Tuya Device ID |
| Child node ID | The exact `node_id` / `cid` used by this gateway |
| Member | The member number from the DP61 command |
| Remote unlock code | The existing eight-digit DP61 code |

After the gateway check, choose **Add a lock manually**, enter the lock Device ID and exact child node ID, then complete the same profile/code form. The node must appear in the gateway's current topology. Repeat for each lock, then select **Save**.

To retrieve missing values, open **Cloud → API Explorer** for your project. **Get Device Information** / **Query Device Details** returns the gateway's `local_key`; device details and gateway sub-device queries provide the lock Device ID and `node_id`. The node ID is not interchangeable with the Device ID. A cloud-reported `ip` can be a public address; obtain the local address from your router. [Tuya device information API](https://developer.tuya.com/en/docs/cloud/7d3f13ae55?id=Kb2rzcwpmvaci).

### Change configuration later

Open the integration entry's menu and choose **Reconfigure**, then select cloud discovery or manual gateway details. Cloud discovery can retrieve a changed gateway key and asks for API credentials again because they are not retained. After the gateway check, add, edit, remove, or rediscover locks and select **Save**. Leaving a code blank when editing keeps the existing code; use **Remove the saved unlock code** to remove it. You can save the gateway with no locks and add them later.

Use **Configure** for the polling interval: the default is 60 seconds, with a supported range of 30–600 seconds. Polling reads connectivity and available battery reports; it does not unlock a device. Removing a lock here does not unpair it from Tuya.

## 7. Unlock from an automation

In **Settings → Automations & scenes**, create an automation. Select your wireless button's press event/device trigger, then add the **Lock: Unlock** action targeting the lock entity. Button triggers vary by integration; use the trigger exposed by your button.

The action in YAML is:

```yaml
actions:
  - action: lock.unlock
    target:
      entity_id: lock.front_door
```

Replace `lock.front_door` with the actual entity ID. The stored remote code is used automatically; keep it out of automation YAML and traces. Use an explicit unlock action rather than a toggle, because the integration does not know the physical lock state. [Home Assistant lock actions](https://www.home-assistant.io/integrations/lock/).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Integration missing after installation | Folder is exactly `config/custom_components/tuya_ble_lock`, then restart Home Assistant and refresh the browser. |
| Cloud permission/authentication error | Correct project credentials and data center; app account linked; API services both subscribed and authorized; trial still active. |
| Gateway or locks missing from cloud | Correct app account, data center, and gateway binding. A phone-only BLE lock is not a gateway child. |
| Gateway cannot connect | Private IP, TCP 6668, correct gateway ID/local key/protocol, no Wi-Fi client isolation. Stop other local clients temporarily; some gateways allow few simultaneous connections. |
| Child offline or missing | Exact node ID; Bluetooth range; batteries; lock listed under this gateway. Wake the lock if its model sleeps. |
| Unlock rejected | Correct currently registered remote code and member. App activity can replace a code on some models. Do not guess codes or repeatedly retry. |
| Unlock timeout / outcome unknown | The command may have executed. Inspect the lock before retrying; no cloud fallback or automatic repeat is performed. |
| Entity state remains Unknown | Expected: availability and command responses do not establish whether the bolt is locked. |
| Stopped working after re-pairing | Re-pairing can change the gateway local key and child identifiers. Retrieve the current values and update the integration. |

For a bug report, include Home Assistant and integration versions, gateway/lock model, protocol version, the error message, and integration diagnostics from **Settings → Devices & services → Tuya BLE Lock → ⋮ → Download diagnostics**. Diagnostics contain only aggregate connectivity information; review attachments before posting. Never include local keys, remote codes, Access Secrets, raw command payloads, account identifiers, or unredacted Tuya exports.

## Privacy and security

Local keys and remote codes are stored in Home Assistant's configuration storage. Treat access to the Home Assistant host and its backups as access to the locks. Anyone permitted to run the entity's unlock action can use the saved code. Restrict access accordingly and review any voice-assistant exposure.

The integration's runtime communicates with the configured gateway locally. The gateway and mobile app can still communicate with Tuya independently; using this integration does not disable those connections. Internet blocking and firmware behavior depend on the gateway and are not guaranteed by this project.

## License

[MIT](LICENSE).

For development, tests, and hardware-support contributions, see [CONTRIBUTING.md](CONTRIBUTING.md).
