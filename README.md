# Tuya BLE Lock for Home Assistant

Open compatible Tuya Bluetooth drawer and cabinet locks from Home Assistant, including from a dashboard or a wireless-button automation. Each lock gets its own device page, unlock control, battery level, and reported locked/unlocked state.

You need **Home Assistant 2026.11.0 or newer**, a compatible lock, and a **Wi-Fi/Bluetooth gateway** (also called a hub). The gateway connects the Bluetooth locks to your home network. Home Assistant does not need a Bluetooth adapter.

```text
Home Assistant → home network → gateway → Bluetooth lock
```

Setup uses your Tuya account to find the devices and retrieve connection details. For the supported T1 / HX201 lock, it also imports the existing unlock credential from recent app-command logs. After setup, this integration sends commands and receives updates directly through your home network.

## Supported devices

These devices have been verified together:

| Device | Name on the product | Name in Tuya | Tuya Product ID |
| --- | --- | --- | --- |
| Gateway | **MOES BHUB-C** — Wireless Smart Gateway (BLE) | `CR3L Bluetooth Gateway` | `csvxfq3ecmedidkh` |
| Drawer/cabinet lock | **T1 tuya** — "Smart Drawer Switch" | `HX201_BLE_LOCKER` | `ahz7wpy9` |

The **Product ID** identifies a model. A **Device ID** identifies one particular gateway or lock; those are the IDs requested during setup. You can check them in the device details on the Tuya Developer Platform described below.

Other Tuya locks are not automatically compatible. They must use the same remote-unlock command format; see [compatibility details](#compatibility-details). The lock closes itself: this integration supports unlocking, not a remote **Lock** command.

## Where to start

| What you want to do | Follow this guide |
| --- | --- |
| Install the integration and add your first locks | [First-time setup](#first-time-setup) |
| Add new locks to a gateway already in Home Assistant | [Add more locks to an existing gateway](#add-more-locks-to-an-existing-gateway) |
| Automatic unlock setup cannot find a recent app command | [Complete automatic unlock setup](#complete-automatic-unlock-setup) |
| Enter an unlock credential manually | [Manual unlock-information guide](docs/remote-unlock-code.md) |
| Understand the advanced lock fields | [Fill in the lock form](#fill-in-the-lock-form) |
| Change an existing lock's code or state setting | [Change a lock's settings](#change-a-locks-settings) |

The instructions use English button names. Automatic setup saves when you submit the last lock's name and area.

## First-time setup

Follow these sections in order. If your devices already work through the gateway in Tuya Smart or Smart Life, start with installation. If the integration is already installed, start with [Tuya cloud access](#prepare-tuya-cloud-access).

### Set up the devices in the phone app

1. Install **Tuya Smart** or **Smart Life**, whichever your device manual specifies. Create an account, sign in, and create a home if asked.
2. Connect your phone to the Wi-Fi network intended for the gateway, usually **2.4 GHz**. Enable Bluetooth and the nearby-device permissions requested by the app.
3. Follow the gateway manual to put it in pairing mode. In the app, use **+ → Add Device** to add the gateway and connect it to Wi-Fi.
4. Put each lock in pairing mode and add it in the same app. Complete any owner/administrator setup requested by the lock.
5. Open the **gateway's device page** in the app. Use **Add sub-device** or **Add device by list** to associate the locks with it; labels vary by gateway. Each lock must appear in the gateway's list of devices.
6. Stand beside a lock, turn off Bluetooth in your phone's settings, and unlock it from the app. Repeat for each lock. This checks that commands work through the gateway, rather than directly from the phone.

**Before continuing:** each lock must open with the phone's Bluetooth off. If it does not, check its batteries, distance from the gateway, and the device manual. Adding a lock only in the phone's Bluetooth settings is insufficient. See Tuya's [gateway setup](https://support.tuya.com/en/help/_detail/K9hut3ffwx4az) and [gateway device-binding guide](https://developer.tuya.com/en/docs/iot-device-dev/tuyaos-gateway-join?id=Kc3234gqbhjjt).

### Install the integration

Choose **one** method. Installation makes Tuya BLE Lock available in Home Assistant; you will add your gateway and locks afterward.

#### Marketplace

The Marketplace is built into Home Assistant and replaces HACS. Use an administrator account.

1. Open **Settings → Marketplace** and accept the community-content notice if prompted.
2. Select **Add from link** and enter `https://github.com/itorgov/ha-tuya-ble-lock`.
3. If asked, connect your GitHub account using the displayed link and code, then return to Home Assistant.
4. Select **Add**. If a content type is requested, choose **Integration**.
5. Open **Tuya BLE Lock**, select **Install**, review the version, and confirm.
6. Restart if Home Assistant requests it. If the gateway setup opens immediately, you can close it and return after collecting the Tuya details below.

See the [official Marketplace guide](https://www.home-assistant.io/integrations/marketplace/#custom-repositories). If upgrading from HACS, existing installations migrate automatically; you do not need to add them again.

<details>
<summary>Manual installation without the Marketplace</summary>

1. On [this repository's GitHub page](https://github.com/itorgov/ha-tuya-ble-lock), choose **Code → Download ZIP** and extract it on your computer.
2. Open the **Home Assistant configuration folder** on the machine running Home Assistant. This is the folder containing `configuration.yaml`, normally `/config` on Home Assistant OS.
3. Create a `custom_components` folder there if it does not exist. From the downloaded files, copy the entire `custom_components/tuya_ble_lock` folder into it.
4. Check the resulting structure, then restart Home Assistant:

   ```text
   config/
     configuration.yaml
     custom_components/
       tuya_ble_lock/
         __init__.py
         manifest.json
         ...
   ```

If Home Assistant OS runs on a separate server, the files must be copied to **that server**, not left on your computer. Use your existing file-access method, or follow Home Assistant's [file-access instructions](https://www.home-assistant.io/common-tasks/os/#configuring-access-to-files), which cover Samba and file-editor apps.

Home Assistant installs the required Python packages automatically. You do not need to install TinyTuya yourself on the Home Assistant server or edit `configuration.yaml`.

</details>

### Prepare Tuya cloud access

The **Tuya Developer Platform** is a website where you give this integration permission to read your device list. Its account is separate from your phone-app account. A **cloud project** is the container for those permissions; creating one does not require writing code.

If you already have a project linked to the correct app account, reuse it. Check that the gateway and all intended locks appear under **Devices → All Devices**, then collect the values below.

1. Register or sign in at the [Tuya Developer Platform](https://platform.tuya.com/).
2. Open **Cloud → Development** (sometimes **Project Management**) → **Create Cloud Project**. Give it a name and choose development method **Smart Home**.
3. Choose the **data center**, meaning the Tuya server region that holds your app account's devices. Find the account region in the phone app under **Me → Settings → Account and Security → Region**, then use [Tuya's region table](https://developer.tuya.com/en/docs/iot/oem-app-data-center-distributed?id=Kafi0ku9l07qb). Your current physical location is not necessarily the account region.
4. Open the project's **Service API** tab. Check that **IoT Core** is authorized and its trial or subscription has not expired. If authorization is missing, use **Go to Authorize**. If the service is unavailable, open **Cloud → Cloud Services**, find IoT Core, and activate an available trial or subscription before authorizing the project. See [Tuya's service-permission guide](https://developer.tuya.com/en/docs/iot/applying-for-api-group-permissions?id=Ka6vf012u6q76).
5. In the project, open **Devices → Link Tuya App Account → Add App Account**. Scan the QR code with the scanner **inside Tuya Smart or Smart Life**, using the same account that owns the devices, and confirm on the phone.
6. Choose automatic device linking if offered. If you choose devices manually, include the gateway and every intended lock. Open **All Devices**, select the correct data center, and check that they appear. See [Tuya's account-linking guide](https://developer.tuya.com/en/docs/iot/link-devices?id=Ka471nu1sfmkl).

Some older Smart Home setup screens also offer **Industry Basic Service**, **Smart Home Basic Service**, and **Device Status Notification**. Accept the wizard's offered authorizations if they appear. The service list varies; these names are not mandatory separate menu items, and your project may list only IoT Core.

Keep these values handy, preferably in a password manager:

| Value | Where to find it | What you will use it for |
| --- | --- | --- |
| **Access ID / Client ID** | Project → **Overview** → authorization details | Home Assistant's Tuya discovery form |
| **Access Secret / Client Secret** | Beside the Access ID | The same discovery form; treat it as a password |
| **Project data center** | The data center where the project's device list shows your devices | Select the same region in Home Assistant |
| **Gateway device ID** | Project → **Devices → All Devices** → your gateway's details | Identifies the hub; do not copy a lock's ID here |

These are **not** your mobile-app login and password. Home Assistant does not save the project credentials, so you will need them again when discovering additional locks.

Setup needs permission to read gateway details, its linked locks, each lock's supported commands, and recent command logs for automatic unlock setup. An active IoT Core subscription does not guarantee access to every query. If setup reports a permission error, use the failed step and error code to check the corresponding service's subscription and project authorization; see [troubleshooting](#troubleshooting). Expired cloud access prevents cloud setup but does not by itself stop already configured local unlocks.

### Find the gateway address

Open your router's app or administration page and find its **Connected devices**, **Clients**, or **DHCP** list. Find the gateway and note its local IP address, such as `192.168.1.50` (an example, not a value to copy). Use the router's address-reservation option to keep it from changing. This must be the gateway's address, not Home Assistant's address or your public internet address.

Automatic setup retrieves the supported gateway's connection details and each T1 / HX201 lock's unlock information. You normally do not need to find a key, select a protocol version, or copy a code.

### Add the gateway in Home Assistant

1. Open **Settings → Devices & services → Add integration** and search for **Tuya BLE Lock**.
2. Choose **Automatic setup**.
3. On **Connect your gateway**, enter the four values from the [cloud-access table](#prepare-tuya-cloud-access) and the **Gateway IP address** from your router. Check **Project data center** carefully: each new setup starts with **Central Europe**, which may be wrong for your account. Retries in the same setup keep your selected region. Select **Submit**.
4. On **Select discovered locks**, select the T1 / HX201 locks you want to add, then submit.
5. On each **Set up…** screen, enter the **Lock name** and optionally select an existing Home Assistant **Area**. Leave Area empty to assign it later. Submit to continue to the next lock. If setup asks you to unlock a lock in the app, follow [these steps](#complete-automatic-unlock-setup), then continue.
6. Submitting the last lock finishes setup and saves all selected locks. Closing the dialog before that discards your changes.

**You should now see:** the gateway and a separate device for every added lock under **Tuya BLE Lock**. Each lock has a control and a battery sensor. Home Assistant calls these individual controls and readings **entities**. Continue with [test an unlock](#test-an-unlock).

Automatic setup recognizes the gateway and lock models listed under [Supported devices](#supported-devices). Use [advanced manual setup](#manual-gateway-and-lock-entry) for another compatible model.

## Complete automatic unlock setup

For a recognized T1 / HX201 lock, setup imports its existing unlock credential from an app command in the last **30 minutes** and selects the correct state setting automatically. You do not need to choose technical settings or copy a code. Setup reads information only; it does not open the lock.

After import, fill in the lock's name and optional area, then submit. The last lock's form saves the setup. Importing a credential does not test it: [test an unlock](#test-an-unlock) once setup finishes.

If **Prepare this lock for Home Assistant** appears without an error:

1. Check which lock the screen names and stand beside that lock.
2. On your phone, turn off **Bluetooth** and **Wi-Fi** so the app uses mobile data. Keep the gateway connected to Wi-Fi.
3. Unlock that lock once in Tuya Smart or Smart Life and confirm it opens.
4. Allow a few seconds for Tuya's logs to arrive, then return to Home Assistant and select **Submit** to retry. There is nothing to fill in on this retry screen.

If the screen shows a cloud-access error, follow its guidance before retrying. For manual credentials, status-only setup, or another compatible model, close the unfinished setup and start **Advanced manual setup** instead; see [manual gateway and lock entry](#manual-gateway-and-lock-entry).

## Add more locks to an existing gateway

Use this when your gateway already appears under **Tuya BLE Lock** in Home Assistant. Adding a lock in the phone app does **not** automatically add it to this integration. You do not need to reinstall the integration or add the same gateway again.

1. **On the phone:** add each new lock to the existing gateway's device list. Confirm it opens from the app with the phone's Bluetooth off.
2. **On the Tuya website:** open your existing project → **Devices → All Devices**. Confirm the new locks appear in the correct data center. If your app account uses manual device linking, include the new locks in that linked account's device selection.
3. Have the project's Access ID, Access Secret, and data center ready; reuse the same project. Supported T1 / HX201 locks import their unlock information during setup.
4. **In Home Assistant:** open **Settings → Devices & services → Tuya BLE Lock**. Find the existing gateway under **Hubs**. Open the **⋮ menu on that gateway's integration entry** and choose **Reconfigure**. Use the gateway entry, not an individual lock's device page.
5. Choose **Automatic setup**. Enter the Access ID and Secret again, select the correct **Project data center**, and check the saved gateway address. A new Reconfigure dialog starts with Central Europe; retries within it keep your selected region. The gateway ID and connection version are already saved. Submit.
6. Select the new T1 / HX201 locks on **Select discovered locks**. Existing locks are omitted from this list and keep their settings.
7. Give each new lock a name and optional area, then submit. If prompted for an app unlock, follow [automatic unlock setup](#complete-automatic-unlock-setup). Submitting the last lock saves the additions.
8. Open the newly added device pages and [test their unlock controls](#test-an-unlock) while beside the locks.

**If you only see “Polling interval”:** you opened **Configure**, which changes how often the gateway is checked. Close it and use **⋮ → Reconfigure** instead.

**If there are no new locks to select:** check the phone app's gateway list and the Tuya project's device list first. A lock already added to this integration is not offered again. Other models or locks whose Tuya details cannot be read may also be omitted; see [troubleshooting](#troubleshooting).

If **Gateway ready** appears during reconfiguration, select **Submit** to save the gateway connection details and keep your existing locks unchanged. Adding a new gateway with no supported locks leaves setup unsaved.

## Using your locks

### Test an unlock

1. Stand beside the lock.
2. In Home Assistant, open **Settings → Devices & services → Tuya BLE Lock**, then the lock's device page.
3. Click the lock control's **name** to open its popup. Select the **open-padlock / Unlock** control.
4. Check that the lock opens and then closes itself. Reported state changes should appear in **Activity**, and the battery reading should appear once received from the gateway.

The compact device-page row may show a **Lock** button. Use the popup's explicit **Unlock** control; remote locking is unsupported. If an unlock times out, inspect the lock before trying again: the command may have reached it, and the integration does not repeat it automatically.

### Open a lock with a wireless button

In **Settings → Automations & scenes**, create an automation. Choose your button's press as the trigger, then add **Lock: Unlock** as the action and select the lock. The available button triggers depend on that button's integration.

The saved unlock information is used automatically. You do not need to include the secret in the automation. For users editing YAML, the action is:

```yaml
actions:
  - action: lock.unlock
    target:
      entity_id: lock.drawer
```

Replace `lock.drawer` with your lock's entity ID. Use **Unlock**, not a toggle. See [Home Assistant lock actions](https://www.home-assistant.io/integrations/lock/).

### Understand state and Activity

**Locked**, **Unlocked**, and battery percentage come from reports received from the gateway. **Unknown** means no valid report has arrived on the current connection. **Unavailable** means the integration cannot currently reach the gateway or lock.

History and Activity show received state changes, including automatic relocking. **Unlock command acknowledged** means the gateway acknowledged a Home Assistant unlock command; it is separate from a reported physical state change. Changes missed while disconnected are not downloaded later from Tuya.

## Change a lock's settings

To rename a lock or change its area, open that lock's Home Assistant device page and edit the device settings. For its unlock credential or state source, use the advanced form:

1. Open **Settings → Devices & services → Tuya BLE Lock** → the existing gateway's **⋮ → Reconfigure**.
2. Choose **Advanced manual setup**. Keep the saved address and protocol; leave **Gateway Local Key** blank to keep the saved key. Submit.
3. Choose **Edit a lock or its code**, select the lock, and submit.
4. Update [the lock form](#fill-in-the-lock-form). Turn on the DP61 confirmation. To change only the state source, leave both credential fields blank; the saved code stays in place.
5. Submit, then choose **Save** in **Manage locks**.

For a T1 / HX201 lock that opens but shows **Unknown**, select **HX201 / T1 cabinet lock (DP40)** as the state source.

To remove a lock from Home Assistant, use **Remove a lock from this integration** in **Manage locks**, then **Save**. This does not remove it from the phone app or unpair it from the gateway.

## Update the integration

For a Marketplace installation, install the update from **Settings → Updates**, then restart Home Assistant. For a manual installation, replace the entire `custom_components/tuya_ble_lock` folder, then restart.

After restarting, refresh the Home Assistant page in your browser before starting setup. If a form still shows old text or an internal field name such as `host`, close the Home Assistant tab and reopen it. For a manual installation, make sure you also copied the `translations` folder.

Keep the existing gateway entry; its settings, saved codes, and entity IDs are retained. Adding locks uses [Reconfigure](#add-more-locks-to-an-existing-gateway), not an integration update.

## Troubleshooting

Cloud setup errors identify the step that failed, such as reading gateway details or checking a lock's supported commands. They also show a Tuya error code or HTTP status when available. Follow the accompanying guidance and include that step and code when asking for help; never include credentials or the full cloud response.

| What you see | What to do |
| --- | --- |
| Tuya BLE Lock is missing from **Add integration** | Restart Home Assistant and refresh the browser after installation. For manual installation, check that `manifest.json` is directly inside `/config/custom_components/tuya_ble_lock/`. |
| Tuya's device list is empty | Check the linked phone-app account and data center. The project must contain the gateway and the locks you want to add. |
| Cloud login or permission error | Use the project's Access ID/Secret, not your app password. Check the selected data center, linked app account, and service authorization for the failed step. If the error reports an expired subscription or exhausted quota, review that service in the Tuya project before retrying. |
| **No new compatible locks found** | Automatic setup offers recognized T1 / HX201 locks. Check that each new lock appears under this gateway in the phone app and in the cloud project. Already configured locks are omitted. Fix any cloud-access error before retrying; other compatible models require advanced manual setup. |
| **This gateway is already configured** | Close the add-integration flow. Use the existing gateway's **⋮ → Reconfigure**. |
| Only **Polling interval** appears | Close **Configure** and use **⋮ → Reconfigure** to add or edit locks. |
| Gateway connection fails | Check its local IP and protocol. Home Assistant must reach it on the home network; guest Wi-Fi isolation can block this. Temporarily stop other software using the gateway locally. If it was paired again, use cloud discovery to retrieve its new key. |
| Lock is offline | Check batteries, Bluetooth range, and whether it still appears under the gateway in the app. Wake it if the model sleeps. |
| Automatic setup cannot find an unlock command | On **Prepare this lock for Home Assistant**, follow the [app-unlock and retry steps](#complete-automatic-unlock-setup) for the named lock. Manual entry and status-only configuration are available through **Advanced manual setup**. |
| **Check/confirm compatibility** or **invalid credentials** on the lock form | Enable the DP61 confirmation. Paste only the command value for this lock; see the [unlock-information guide](docs/remote-unlock-code.md). |
| Missing code or unlock rejected | Configure the lock's own current unlock information. A new lock added without a code cannot open from Home Assistant. Some app actions can replace the code; retrieve a fresh successful command if necessary. |
| **This profile supports unlocking only** | Open the lock control's popup and choose **Unlock**, not **Lock**. |
| Opens, but state is **Unknown** | For the supported T1 / HX201 lock, check the [DP40 state setting](#change-a-locks-settings). An acknowledgement alone does not establish state. |
| Battery is missing or **Unknown** | Enable its entity if disabled. A value appears after a valid battery report arrives. |
| No Activity or history | Check connectivity and Home Assistant's [Recorder](https://www.home-assistant.io/integrations/recorder/) and [Activity](https://www.home-assistant.io/integrations/logbook/) settings. They are normally enabled, but filters can exclude a lock. |

For help, include the Home Assistant and integration versions, gateway/lock models, the step you reached, and the exact error. Download diagnostics from the integration entry's **⋮ → Download diagnostics** before reloading or restarting; the diagnostic counters reset on reload.

Diagnostics omit credentials and raw commands. Review anything you attach to an issue. Keep Access Secrets, gateway keys, unlock values, and unredacted device exports private.

## Advanced setup

### Find the gateway connection version

Skip this for the verified MOES BHUB-C / CR3L gateway using **3.5**. For another gateway, a [TinyTuya network scan](https://github.com/jasonacox/tinytuya#network-scanner) can report its address, ID, and protocol version.

Run this on a computer with Python installed, connected to the **same home network as the gateway**. It is a discovery scan, not an unlock. On macOS/Linux, open Terminal:

```sh
python3 -m venv tuya-tools
./tuya-tools/bin/python -m pip install tinytuya==1.20.0
./tuya-tools/bin/python -m tinytuya scan
```

On Windows, open PowerShell:

```powershell
py -m venv tuya-tools
.\tuya-tools\Scripts\python.exe -m pip install tinytuya==1.20.0
.\tuya-tools\Scripts\python.exe -m tinytuya scan
```

Match the gateway by its Device ID and use the reported version. For networks with firewall rules, the scan uses UDP ports 6666, 6667, and 7000. The integration's direct connection uses TCP port **6668**; keep that port private to your home network.

### Manual gateway and lock entry

Use this only if you already have the required identifiers and secrets. Cloud-assisted setup above retrieves most of them for you.

For a new gateway, choose **Add integration → Tuya BLE Lock → Advanced manual setup**. For an existing gateway, choose **⋮ → Reconfigure → Advanced manual setup**. Enter or retain the gateway details:

| Value | Meaning |
| --- | --- |
| Gateway IP address | The gateway's private address on your home network |
| Gateway device ID | The gateway's individual Tuya identifier |
| Gateway Local Key | Its exact 16-byte connection key, including punctuation; blank retains an already saved key |
| LAN protocol version | `3.5` for the verified gateway, or the observed `3.3`/`3.4`/`3.5` for another model |

After the gateway check, choose **Add a lock manually**. Enter its name, **Lock device ID**, and **Exact child node_id**. The last value is the lock's identifier inside that gateway; it is different from the Device ID and must be copied exactly, not guessed from a Bluetooth MAC address. Complete [the lock form](#fill-in-the-lock-form), repeat for other locks, then choose **Save**.

To retrieve missing values, open **Cloud → API Explorer** and select your project and its data center:

- **Gateway Local Key:** select **IoT Core → Device Management → Query Device Details** (`GET /v2.0/cloud/thing/{device_id}`). Enter the gateway's Device ID, run the query, and copy `local_key` from `result`. See [Tuya's Query Device Details reference](https://developer.tuya.com/en/docs/cloud/3829469013?id=Kcp2l2v9wma0m).
- **Lock identifiers:** find **Query Zigbee devices under the gateway** (`GET /v1.0/devices/{deviceId}/sub-devices`). This query also returns Bluetooth locks on the supported gateway. Enter the **gateway's** Device ID. For each lock in `result`, copy `id` as its Lock device ID and `node_id` as its child identifier. See [Tuya's sub-device reference](https://developer.tuya.com/en/docs/cloud/fd92a062fa?id=Kd30e9f9fm3w6).

A cloud-reported `ip` may be a public address, so obtain the local address from your router. Keep the Local Key private and copy its value without JSON quotes.

### Fill in the lock form

This is the advanced **Configure lock status and unlock code** screen used for manual entry, other compatible models, and editing an existing lock. Automatic setup for recognized T1 / HX201 locks skips this screen. To enter an unlock credential manually, paste the value obtained in the [manual guide](docs/remote-unlock-code.md).

| Field on the screen | What to do |
| --- | --- |
| **Lock name** | Choose a recognizable name for this lock. |
| **My lock uses the supported DP61 profile** | Turn **on** for the supported T1 / HX201 lock. This confirms that it uses the supported opening-command format. You must confirm this each time you submit this screen. For another model, first check [compatibility](#compatibility-details). |
| **Lock state source** | For the supported T1 / HX201 lock, select **HX201 / T1 cabinet lock (DP40)**. This tells Home Assistant how to read whether it is locked. Cloud discovery normally selects it for the supported Product ID. |
| **Member number** | When pasting a command below, leave this as shown: the correct number is extracted automatically. |
| **Existing eight-digit remote unlock code** | Leave blank when pasting a command below. |
| **Existing DP61 command (Base64)** | Paste **this lock's** copied value, without surrounding quotes or spaces. |
| **Remove the saved unlock code** | Leave **off**. Turning it on removes the code, even if you fill the other credential fields. |

**On this manual form for a T1 lock, both settings are needed:** enable the **DP61** confirmation and select **DP40** as the state source. Automatic setup handles these choices for you.

If you know the eight-digit code and member number already, fill those two fields and leave the Base64 field blank. Keep any leading zeroes. Do not invent a code or assume the member number is `1`.

Select **Submit**. Another selected lock's form may appear; complete it too. When you return to **Manage locks**, select **Save**.

For a **new lock**, leaving both credential fields blank adds status reporting only; Home Assistant cannot unlock it until you supply the code. For an **existing lock**, blank credential fields keep its saved code. Saving this form never opens the lock.

### Compatibility details

The supported unlock format is **HX201-style DP61**, named `remote_no_dp_key`, with an existing eight-digit remote code. Discovery checks Tuya's **device model**, which describes the commands and readings a lock supports. It requires the exact writable unlock command; a Tuya logo or a similar product name alone does not establish support. See the [command-format reference](docs/remote-unlock-code.md) and [hardware-support guidance](CONTRIBUTING.md).

<details>
<summary>Check a lock's command definition in Tuya</summary>

In **Cloud → API Explorer**, select your project and data center, then **IoT Core → Device Control → Query Things Data Model**. Enter the **lock's Device ID** and run `GET /v2.0/cloud/thing/{device_id}/model`.

The response's `result.model` contains the definition as JSON text. Inside `services`, find the default service (`code` is an empty string). Its `properties` list must contain one unambiguous entry with all these fields:

| Field | Required value |
| --- | --- |
| `abilityId` | The number `61` |
| `code` | `remote_no_dp_key` |
| `accessMode` | `rw` or `wr`, allowing commands to be sent |
| `typeSpec.type` | `raw`, meaning Base64-encoded binary data |

See [Tuya's model API reference](https://developer.tuya.com/en/docs/cloud/bd68171262?id=Kcp4utbhzzfgo). This query checks the command definition; the credential comes from [automatic import](#complete-automatic-unlock-setup) or the [manual guide](docs/remote-unlock-code.md).

</details>

The known T1 / HX201 profile reports state through **DP40**. Other compatible locks use **Standard lock state (DP47)** if they report their motor state that way. Manual setup defaults to DP47, so select DP40 explicitly for the supported T1. LAN protocols 3.3 and 3.4 are configurable but have not been verified on hardware with this integration.

The integration does not pair hardware or create lock users, codes, fingerprints, or cards. All of that remains in the manufacturer's app.

## Privacy and security

Gateway keys and lock codes are saved in Home Assistant's configuration. Protect access to the server and its backups. Anyone allowed to run a lock's unlock action can use its saved code, including through automations or exposed voice assistants.

Tuya project credentials are used only during setup and are not saved. Automatic unlock setup reads recent command logs and keeps only the extracted lock credential, not the raw commands. The gateway and phone app may still contact Tuya independently; this integration does not disable their internet connections.

## License and development

[MIT](LICENSE). For development and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).
