# Tuya BLE Lock for Home Assistant

Unlock compatible Tuya Bluetooth cabinet and drawer locks from Home Assistant through a Wi-Fi/Bluetooth gateway. Each lock appears as a separate device with a lock entity and a battery sensor, ready for dashboards, scripts, and automations.

Requires **Home Assistant 2026.10 or newer**.

```text
Home Assistant ── local network ── Tuya gateway ── Bluetooth ── lock
```

Commands and state updates use the local network. Tuya Cloud is used during setup to retrieve gateway credentials and discover locks; its API credentials are not saved. Home Assistant does not need a Bluetooth adapter.

## Supported devices

The following gateway and lock are supported together using Tuya LAN protocol **3.5**:

| Device | Retail brand / model | Model reported by Tuya | Tuya Product ID (PID) |
| --- | --- | --- | --- |
| Wi-Fi/Bluetooth gateway | **MOES BHUB-C** — Wireless Smart Gateway (BLE) | `CR3L Bluetooth Gateway` | `csvxfq3ecmedidkh` |
| Bluetooth drawer/cabinet lock | **T1 tuya** — labeled "Smart Drawer Switch" on the packaging | `HX201_BLE_LOCKER` | `ahz7wpy9` |

Check the Tuya **Product ID** as well as the retail model. It identifies a product profile; the **Device ID** requested during setup identifies your individual device.

## Features and compatibility

This integration supports the **HX201-style DP61 remote-unlock protocol** (`remote_no_dp_key`). Other devices must use this protocol and have an existing eight-digit remote unlock code. Tuya branding or successful discovery alone does not establish compatibility. LAN protocols 3.3 and 3.4 are configurable but have not been verified on hardware with this integration.

- Unlock through the standard Home Assistant `lock.unlock` action.
- View reported locked/unlocked state and battery percentage.
- Record state changes, including automatic relocking, in Home Assistant history and Activity.
- Add multiple gateways, with a separate device for each configured lock.

Locking is handled by the lock itself; a remote locking command is not supported. The integration does not pair devices, create unlock codes, or register users, fingerprints, or cards. It never automatically retries an unlock: if a command times out, inspect the lock before trying again.

The gateway must be reachable at a private IPv4 address from Home Assistant, with TCP port **6668** allowed. Reserve its IP address in your router and do not expose this port to the internet.

## Setup

Follow these steps for a new installation. If you already have the gateway local key, protocol version, lock identifiers, and remote unlock code, go to [installation](#5-install-the-integration), then [manual setup](#manual-setup).

### 1. Set up the gateway and locks in the mobile app

1. Install **Tuya Smart** or **Smart Life**, as specified by the device manufacturer. Register a permanent account, sign in, and create a home if requested. Use the same account throughout setup.
2. Enable Bluetooth and the app's requested nearby-device permissions. Connect your phone to the gateway's supported Wi-Fi network, normally **2.4 GHz**.
3. Power the gateway and put it in pairing mode following its manual. In the app, choose **+ → Add Device** and add the Bluetooth gateway; the manual category is usually **Gateway Control → Wireless Gateway (Bluetooth)**. Enter your Wi-Fi credentials and finish pairing. [Tuya's gateway instructions](https://support.tuya.com/en/help/_detail/K9hut3ffwx4az).
4. Put each lock in pairing mode and add it **inside the app**, not in the phone's Bluetooth settings. Finish any administrator/owner setup required by the lock. [Tuya's lock instructions](https://support.tuya.com/en/help/_detail/K9hw4ebtzv7rb).
5. Open the gateway's device page and add/bind each lock through **Add sub-device** or **Add device by list**, depending on the gateway panel. Verify that each lock is listed under that gateway. Follow the product manual if its binding sequence differs. [Tuya's sub-device workflow](https://developer.tuya.com/en/docs/iot-device-dev/tuyaos-gateway-join?id=Kc3234gqbhjjt).
6. While beside the lock, disable your phone's Bluetooth and unlock through the app. This checks the gateway connection. If it fails, resolve pairing, range, batteries, or remote-unlock settings before continuing. Note the time of a successful unlock; you may need its command log in step 4.

### 2. Create and authorize a Tuya cloud project

The developer account is separate from the mobile-app account. Linking the app in the next step gives the project access to your devices.

1. Register/sign in at the [Tuya Developer Platform](https://platform.tuya.com/).
2. Open **Cloud → Development** (also shown as **Project Management**) and select **Create Cloud Project**.
3. Enter a name, choose **Smart Home** as the development method, and select the data center serving your **app account's registered region**. Check that region in the app under **Me → Settings → Account and Security → Region**. Use [Tuya's region/data-center mapping](https://developer.tuya.com/en/docs/iot/oem-app-data-center-distributed?id=Kafi0ku9l07qb), including its notes for older accounts; do not infer the data center from your current location.
4. Accept the Smart Home API services offered by the wizard. Tuya currently lists **Industry Basic Service**, **Smart Home Basic Service**, and **Device Status Notification**. Copy the project's **Access ID / Client ID** and **Access Secret / Client Secret** from **Overview** into a password manager. [Official project wizard](https://developer.tuya.com/en/docs/iot/Platform_Configuration_smarthome?id=Kamcgamwoevrx).
5. Under **Service API**, ensure the project is authorized for **IoT Core** and **Authorization** / **Authorization Token Management**, plus the device-management services above. If missing, use **Go to Authorize**. Subscribing to a service and authorizing this project are separate steps. You can inspect available services under **Cloud → Cloud Services → My Service**. [API authorization instructions](https://developer.tuya.com/en/docs/iot/applying-for-api-group-permissions?id=Ka6vf012u6q76).

Tuya can change service names, eligibility, quotas, and trial terms. Check the subscriptions shown for your account; a developer project does not guarantee permanent free API access. Expired cloud access affects discovery and credential retrieval, not already configured local commands while the local credentials remain valid.

### 3. Link the mobile-app account and collect the gateway details

1. In your project, open **Devices → Link Tuya App Account → Add App Account**. Use this option for Tuya Smart/Smart Life, not **Link My App**, which is for apps you develop.
2. Scan the displayed QR code with the scanner **inside the same mobile app/account** used in step 1. Confirm the authorization on the phone.
3. Select automatic device linking, or explicitly include the gateway and all intended locks if using manual linking.
4. Open **All Devices**, select the matching data center, and confirm that both the gateway and locks appear. If the list is empty, check the app account, data center, and linking permissions. [Official account-linking instructions](https://developer.tuya.com/en/docs/iot/link-devices?id=Ka471nu1sfmkl).
5. Copy the **gateway's Device ID**. This is different from each lock's Device ID. Find the gateway's local IP address in your router's client/DHCP list and reserve it.
6. Obtain its LAN protocol version. If you do not know it, run a read-only [TinyTuya network scan](https://github.com/jasonacox/tinytuya#network-scanner) on a computer on the same LAN:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install tinytuya==1.20.0
   python -m tinytuya scan
   ```

   On Windows, activate with `.venv\Scripts\activate`. The scan reports the gateway ID, IP, and version. Broadcast discovery needs UDP 6666, 6667, and 7000 on that LAN; the integration itself connects directly to the configured IP.

### 4. Obtain each lock's remote unlock code

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
3. Find the **outgoing command** for `remote_no_dp_key` / DP61, not its short response. Copy its Base64 value privately.
4. During lock configuration, paste it into **Existing DP61 command (Base64)**; the integration extracts the member and code locally. Alternatively, enter the code and member directly.

See the [detailed log-query instructions](docs/remote-unlock-code.md) for the API request and supported command format. If your product hides this value, uses a different format, or changes the code, you need a currently valid compatible code before this integration can unlock it.

### 5. Install the integration

Choose HACS or manual installation. HACS is optional.

#### HACS

1. [Install and configure HACS](https://www.hacs.dev/docs/use/download/download/) if needed.
2. In **HACS → ⋮ → Custom repositories**, add `https://github.com/itorgov/ha-tuya-ble-lock` with type **Integration**.
3. Find **Tuya BLE Lock**, download it, and restart Home Assistant. See [HACS custom repository instructions](https://www.hacs.dev/docs/faq/custom_repositories/).

#### Manual installation

1. Download this repository and copy `custom_components/tuya_ble_lock` into your Home Assistant configuration directory:

   ```text
   config/
     custom_components/
       tuya_ble_lock/
         __init__.py
         manifest.json
         ...
   ```

2. Restart Home Assistant. It installs the integration's Python dependencies automatically; no manual TinyTuya installation inside Home Assistant is needed.

### 6. Add the gateway and its locks

1. Open **Settings → Devices & services → Add integration** and search for **Tuya BLE Lock**.
2. Choose **Discover locks with Tuya Cloud**. Enter the project's Access ID, Secret, data center, and the **gateway Device ID**. This retrieves the gateway local key and its child devices.
3. Enter the gateway's **local IP** and verified **LAN protocol version**. Leave the local-key field blank to retain the imported key.
4. Select the compatible locks you want to add. For each lock, confirm the supported DP61 profile, choose **Lock state source** using the table below, and enter its remote code and member number or paste its Base64 command from step 4.
5. Select **Save** in the lock-management menu. Each configured lock has its own device and `lock` entity, linked to the gateway. You can leave a code blank to complete discovery, but unlocking remains unavailable until you supply it. Repeat for another gateway.

| Lock state source | When to use it |
| --- | --- |
| **HX201 / T1 cabinet lock (DP40)** | The T1 / HX201 lock listed under Supported devices. Cloud discovery selects this for Product ID `ahz7wpy9`; select it yourself during manual setup. |
| **Standard lock state (DP47)** | Other compatible locks that report their motor state through DP47. This is the default for manual setup. |

Setup does not unlock anything. Make the first [unlock test](#unlock-from-the-interface) while beside the lock.

### Manual setup

Open **Settings → Devices & services → Add integration → Tuya BLE Lock** and choose **Enter local gateway details manually**. You need:

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

After the gateway check, choose **Add a lock manually**, enter the lock Device ID and exact child node ID, then complete the profile/code form using the state-source table above. The lock must appear in the gateway's device list. Repeat for each lock, then select **Save**.

To retrieve missing values, open **Cloud → API Explorer** for your project. **Get Device Information** / **Query Device Details** returns the gateway's `local_key`; device details and gateway sub-device queries provide the lock Device ID and `node_id`. The node ID is not interchangeable with the Device ID. A cloud-reported `ip` can be a public address; obtain the local address from your router. [Tuya device information API](https://developer.tuya.com/en/docs/cloud/7d3f13ae55?id=Kb2rzcwpmvaci).

## Using your locks

### Unlock from the interface

Click the lock entity's **name** and select the **open-padlock / Unlock** control. Alternatively, open **Developer tools → Actions → Lock: Unlock** and select the entity.

The compact entity row may offer **Lock**. That action is unsupported; use the explicit **Unlock** control.

### Unlock from an automation

In **Settings → Automations & scenes**, create an automation. Select your wireless button's press event/device trigger, then add the **Lock: Unlock** action targeting the lock entity. Button triggers vary by integration; use the trigger exposed by your button.

The action in YAML is:

```yaml
actions:
  - action: lock.unlock
    target:
      entity_id: lock.drawer
```

Replace `lock.drawer` with your entity ID. The stored remote code is used automatically; do not put it in automation YAML. Use `lock.unlock` rather than a toggle, which can request unsupported locking. See [Home Assistant lock actions](https://www.home-assistant.io/integrations/lock/).

### State, battery, and Activity

Lock state and battery percentage come from gateway reports. **Unknown** means no valid value has been received on the current connection. State updates arrive independently of the periodic refresh interval; reported app unlocks and automatic relocks can also update the entity.

Home Assistant records locked/unlocked transitions in history and Activity. A successful Home Assistant unlock command also adds **Unlock command acknowledged**, which indicates command acceptance rather than confirmed movement. Physical state changes depend on reports from the selected lock state source.

History and Activity require [Recorder](https://www.home-assistant.io/integrations/recorder/) and [Activity (`logbook`)](https://www.home-assistant.io/integrations/logbook/), normally enabled in the default Home Assistant configuration. Their filters can exclude entities or events. Changes missed while disconnected are not retrieved from Tuya Cloud.

## Managing the integration

### Change gateway or lock settings

Open the integration entry's menu and choose **Reconfigure**. Select **Enter local gateway details manually** to keep your saved gateway settings without cloud credentials, or use cloud discovery to retrieve a changed gateway key and discover locks.

After the gateway check, add, edit, remove, or rediscover locks. To change a lock's state source or remote code, choose **Edit a lock or its code**. Leave key/code fields blank to retain saved values; use **Remove the saved unlock code** when removal is intended. Finish with **Save**. Removing a lock from the integration does not unpair it from Tuya.

Use **Configure** to change the status-refresh interval, from 30–600 seconds (default: 60). Incoming state reports are processed between refreshes.

### Update the integration

Update through HACS, or replace the entire `custom_components/tuya_ble_lock` folder with the new version, then restart Home Assistant. Keep the existing integration entry to preserve its settings and entity IDs.

Updates preserve each lock's selected state source. If a T1 / HX201 lock shows **Unknown**, use [Reconfigure](#change-gateway-or-lock-settings) to select **HX201 / T1 cabinet lock (DP40)**.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Integration missing after installation | Folder is exactly `config/custom_components/tuya_ble_lock`, then restart Home Assistant and refresh the browser. |
| Cloud permission/authentication error | Correct project credentials and data center; app account linked; API services both subscribed and authorized; trial still active. |
| Gateway or locks missing from cloud | Correct app account, data center, and gateway binding. A phone-only BLE lock is not a gateway child. |
| Gateway cannot connect | Private IP, TCP 6668, correct gateway ID/local key/protocol, no Wi-Fi client isolation. Stop other local clients temporarily; some gateways allow few simultaneous connections. |
| Child offline or missing | Exact node ID; Bluetooth range; batteries; lock listed under this gateway. Wake the lock if its model sleeps. |
| Device page shows Lock / "This profile supports unlocking only" | Click the lock entity's name, then use the open-padlock / **Unlock** control in its more-info dialog, or run **Lock: Unlock** from Developer tools → Actions. |
| Unlock rejected | Correct currently registered remote code and member. App activity can replace a code on some models. Do not guess codes or repeatedly retry. |
| Unlock timeout / outcome unknown | The command may have executed. Inspect the lock before retrying; no cloud fallback or automatic repeat is performed. |
| Lock state remains Unknown | Check the [lock state source](#6-add-the-gateway-and-its-locks) and gateway connectivity. A command acknowledgement alone cannot establish lock state. |
| Battery missing or Unknown | Enable the battery entity if you disabled it. A value requires a valid battery report (DP8) from the gateway. |
| Activity or history entries missing | Check Recorder/Activity filters and connectivity. Command entries require a successful Home Assistant unlock acknowledgement; state entries require gateway state reports. |
| Stopped working after re-pairing | Re-pairing can change the gateway local key and child identifiers. Retrieve the current values and update the integration. |

For a bug report, include the Home Assistant and integration versions, gateway/lock models, LAN protocol version, steps to reproduce, and the error message. Download diagnostics from **Settings → Devices & services → Tuya BLE Lock → ⋮ → Download diagnostics** before restarting or reloading, since report counters reset on reload.

Diagnostics include anonymous report counters and exclude credentials and raw commands. Review attachments before posting. Never share local keys, remote codes, Access Secrets, account identifiers, or unredacted Tuya exports. Technical details for troubleshooting and contributing hardware support are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Privacy and security

Local keys and remote codes are stored in Home Assistant's configuration storage. Treat access to the Home Assistant host and its backups as access to the locks. Anyone permitted to run the entity's unlock action can use the saved code. Restrict access accordingly and review any voice-assistant exposure.

The integration's runtime communicates with the configured gateway locally. The gateway and mobile app can still communicate with Tuya independently; using this integration does not disable those connections. Internet blocking and firmware behavior depend on the gateway and are not guaranteed by this project.

## License

[MIT](LICENSE).

For development, tests, and hardware-support contributions, see [CONTRIBUTING.md](CONTRIBUTING.md).
