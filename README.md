# Tuya BLE Lock for Home Assistant

Open compatible Tuya Bluetooth drawer and cabinet locks from Home Assistant, including from a dashboard or a wireless-button automation. Each lock gets its own device page, unlock control, battery level, and reported locked/unlocked state.

You need **Home Assistant 2026.10 or newer**, a compatible lock, and a **Wi-Fi/Bluetooth gateway** (also called a hub). The gateway connects the Bluetooth locks to your home network. Home Assistant does not need a Bluetooth adapter.

```text
Home Assistant → home network → gateway → Bluetooth lock
```

Setup uses your Tuya account to find the devices and retrieve connection details. After setup, this integration sends commands and receives updates directly through your home network.

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
| Find the value needed to make a lock open | [Find your lock's unlock information](docs/remote-unlock-code.md) |
| Understand the fields on the lock setup screen | [Fill in the lock form](#fill-in-the-lock-form) |
| Change an existing lock's code or state setting | [Change a lock's settings](#change-a-locks-settings) |

The instructions use English button names. **Submit** advances a form; **Save** in the final **Manage locks** menu saves the whole setup.

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

#### Home Assistant 2026.11 or newer: Marketplace

The Marketplace is built into Home Assistant and replaces HACS. Use an administrator account.

1. Open **Settings → Marketplace** and accept the community-content notice if prompted.
2. Select **Add from link** and enter `https://github.com/itorgov/ha-tuya-ble-lock`.
3. If asked, connect your GitHub account using the displayed link and code, then return to Home Assistant.
4. Select **Add**. If a content type is requested, choose **Integration**.
5. Open **Tuya BLE Lock**, select **Install**, review the version, and confirm.
6. Restart if Home Assistant requests it. If the gateway setup opens immediately, you can close it and return after collecting the Tuya details below.

See the [official Marketplace guide](https://www.home-assistant.io/integrations/marketplace/#custom-repositories). If upgrading from HACS, existing installations migrate automatically; you do not need to add them again.

#### Home Assistant 2026.10: HACS

HACS is the community integration manager used before the Marketplace. If you do not have it on Home Assistant 2026.10, follow the [HACS installation and setup guide](https://www.hacs.xyz/docs/use/).

1. Open **HACS** in Home Assistant.
2. Open its **⋮** menu → **Custom repositories**.
3. Enter `https://github.com/itorgov/ha-tuya-ble-lock`, choose type **Integration**, and select **Add**.
4. Find **Tuya BLE Lock** in HACS, open it, and select **Download**.
5. Restart Home Assistant.

See the [HACS custom-repository guide](https://www.hacs.xyz/docs/faq/custom_repositories/) if the menu is unfamiliar.

<details>
<summary>Manual installation without HACS</summary>

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
4. Authorize the services offered by the Smart Home wizard: **Industry Basic Service**, **Smart Home Basic Service**, and **Device Status Notification**. See [Tuya's project-creation guide](https://developer.tuya.com/en/docs/iot/Platform_Configuration_smarthome?id=Kamcgamwoevrx).
5. In the project, open **Devices → Link Tuya App Account → Add App Account**. Scan the QR code with the scanner **inside Tuya Smart or Smart Life**, using the same account that owns the devices, and confirm on the phone.
6. Choose automatic device linking if offered. If you choose devices manually, include the gateway and every intended lock. Open **All Devices**, select the correct data center, and check that they appear. See [Tuya's account-linking guide](https://developer.tuya.com/en/docs/iot/link-devices?id=Ka471nu1sfmkl).

Keep these values handy, preferably in a password manager:

| Value | Where to find it | What you will use it for |
| --- | --- | --- |
| **Access ID / Client ID** | Project → **Overview** → authorization details | Home Assistant's Tuya discovery form |
| **Access Secret / Client Secret** | Beside the Access ID | The same discovery form; treat it as a password |
| **Project data center** | The data center where the project's device list shows your devices | Select the same region in Home Assistant |
| **Gateway device ID** | Project → **Devices → All Devices** → your gateway's details | Identifies the hub; do not copy a lock's ID here |

These are **not** your mobile-app login and password. Home Assistant does not save the project credentials, so you will need them again when discovering additional locks.

If Tuya reports missing permissions, open the project's **Service API → Go to Authorize**. Check that the required services are both available to your account and authorized for this project; device access may be listed under **IoT Core**, and token access under **Authorization**. Tuya's [service-permission guide](https://developer.tuya.com/en/docs/iot/applying-for-api-group-permissions?id=Ka6vf012u6q76) explains the two steps. Service names, trial periods, and availability can vary. Expired cloud access prevents discovery but does not by itself stop already configured local unlocks.

### Collect the gateway address and each lock's unlock information

**Gateway address:** open your router's app or administration page and find its **Connected devices**, **Clients**, or **DHCP** list. Find the gateway and note its local IP address, such as `192.168.1.50` (an example, not a value to copy). Use the router's address-reservation option to keep it from changing. This must be the gateway's address, not Home Assistant's address or your public internet address.

**Gateway connection version:** for the verified **MOES BHUB-C / CR3L** gateway above, use **3.5**. For another gateway, see [find the gateway connection version](#find-the-gateway-connection-version).

**Each lock's unlock information:** discovery finds locks but does not retrieve the secret needed to open them. Follow [Find your lock's unlock information](docs/remote-unlock-code.md) for **each lock**. That guide shows how to copy a value from your own successful app unlock. Keep the value together with the name of the lock it belongs to; do not reuse another lock's value.

You do not need to decode that value or understand the term *Base64*. You will paste it into **Existing DP61 command (Base64)**. If you already know the lock's existing eight-digit remote unlock code **and** its member number, you can enter those instead. The code is not your Tuya password, the gateway key, or necessarily a keypad PIN.

### Add the gateway in Home Assistant

1. Open **Settings → Devices & services → Add integration** and search for **Tuya BLE Lock**.
2. Choose **Discover locks with Tuya Cloud**.
3. On **Read gateway details from Tuya**, enter the four values from the [cloud-access table](#prepare-tuya-cloud-access). Check **Project data center** carefully: the form initially selects **Central Europe**, which may be wrong for your account. Select **Submit**.
4. On **Connect to the local gateway**, fill in:

   | Field | What to enter |
   | --- | --- |
   | **Gateway name** | A name you will recognize, such as `Living room gateway` |
   | **Gateway IPv4 address** | The gateway's local IP address from your router |
   | **LAN protocol version** | **3.5** for the verified MOES gateway above |
   | **Gateway Local Key** | **Leave blank.** The preceding cloud step has already retrieved it. This key is the gateway's connection password. |

5. Select **Submit**. On **Select discovered locks**, select the locks you want to add, then submit.
6. Complete [the lock form](#fill-in-the-lock-form) for each selected lock. Check the name each time so you paste the correct lock's unlock information.
7. When **Manage locks** appears, choose **Save**. Closing the dialog before this step discards your changes.

**You should now see:** the gateway and a separate device for every added lock under **Tuya BLE Lock**. Each lock has a control and a battery sensor. Home Assistant calls these individual controls and readings **entities**. Continue with [test an unlock](#test-an-unlock).

## Fill in the lock form

This is the **Configure lock status and unlock code** screen shown when adding or editing a lock. The simplest method is to paste the value obtained in the [unlock-information guide](docs/remote-unlock-code.md).

| Field on the screen | What to do |
| --- | --- |
| **Lock name** | Choose a recognizable name for this lock. |
| **My lock uses the supported DP61 profile** | Turn **on** for the supported T1 / HX201 lock. This confirms that it uses the supported opening-command format. You must confirm this each time you submit this screen. For another model, first check [compatibility](#compatibility-details). |
| **Lock state source** | For the supported T1 / HX201 lock, select **HX201 / T1 cabinet lock (DP40)**. This tells Home Assistant how to read whether it is locked. Cloud discovery normally selects it for the supported Product ID. |
| **Member number** | When pasting a command below, leave this as shown: the correct number is extracted automatically. |
| **Existing eight-digit remote unlock code** | Leave blank when pasting a command below. |
| **Existing DP61 command (Base64)** | Paste **this lock's** copied value, without surrounding quotes or spaces. |
| **Remove the saved unlock code** | Leave **off**. Turning it on removes the code, even if you fill the other credential fields. |

**For the supported T1 lock, both settings are needed:** enable the **DP61** confirmation and select **DP40** as the state source. They control different things; they are not alternatives.

If you know the eight-digit code and member number already, fill those two fields and leave the Base64 field blank. Keep any leading zeroes. Do not invent a code or assume the member number is `1`.

Select **Submit**. Another selected lock's form may appear; complete it too. When you return to **Manage locks**, select **Save**.

For a **new lock**, leaving both credential fields blank adds status reporting only; Home Assistant cannot unlock it until you supply the code. For an **existing lock**, blank credential fields keep its saved code. Saving this form never opens the lock.

## Add more locks to an existing gateway

Use this when your gateway already appears under **Tuya BLE Lock** in Home Assistant. Adding a lock in the phone app does **not** automatically add it to this integration. You do not need to reinstall the integration or add the same gateway again.

1. **On the phone:** add each new lock to the existing gateway's device list. Confirm it opens from the app with the phone's Bluetooth off.
2. **On the Tuya website:** open your existing project → **Devices → All Devices**. Confirm the new locks appear in the correct data center. If your app account uses manual device linking, include the new locks in that linked account's device selection.
3. Obtain [unlock information](docs/remote-unlock-code.md) for each **new** lock. Have the project's Access ID, Access Secret, and data center ready too; reuse the same project.
4. **In Home Assistant:** open **Settings → Devices & services → Tuya BLE Lock**. Find the existing gateway under **Hubs**. Open the **⋮ menu on that gateway's integration entry** and choose **Reconfigure**. Use the gateway entry, not an individual lock's device page.
5. Choose **Discover locks with Tuya Cloud**. Enter the Access ID and Secret again and select the correct **Project data center**; it defaults to Central Europe each time. The gateway ID is already saved, so this form does not ask for it again.
6. On **Connect to the local gateway**, keep the saved gateway name, address, and protocol version unless they changed. Leave **Gateway Local Key** blank and submit.
7. Select the new locks on **Select discovered locks**. Existing locks are deliberately omitted from this list; they keep their settings.
8. Complete [the lock form](#fill-in-the-lock-form) for each new lock, then select **Save** in **Manage locks**.
9. Open the newly added device pages and [test their unlock controls](#test-an-unlock) while beside the locks.

**If you only see “Polling interval”:** you opened **Configure**, which changes how often the gateway is checked. Close it and use **⋮ → Reconfigure** instead.

**If there are no new locks to select:** check the phone app's gateway list and the Tuya project's device list first. A lock already added to this integration is not offered again. Other models or locks whose Tuya details cannot be read may also be omitted; see [troubleshooting](#troubleshooting).

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

1. Open **Settings → Devices & services → Tuya BLE Lock** → the existing gateway's **⋮ → Reconfigure**.
2. Choose **Enter local gateway details manually**. Keep the saved address and protocol; leave **Gateway Local Key** blank to keep the saved key. Submit.
3. Choose **Edit a lock or its code**, select the lock, and submit.
4. Update [the lock form](#fill-in-the-lock-form). Turn on the DP61 confirmation. To change only the state source, leave both credential fields blank; the saved code stays in place.
5. Submit, then choose **Save** in **Manage locks**.

For a T1 / HX201 lock that opens but shows **Unknown**, select **HX201 / T1 cabinet lock (DP40)** as the state source.

To remove a lock from Home Assistant, use **Remove a lock from this integration** in **Manage locks**, then **Save**. This does not remove it from the phone app or unpair it from the gateway.

## Update the integration

For a Marketplace installation, install the update from **Settings → Updates**, then restart Home Assistant. On Home Assistant 2026.10, update through HACS. For a manual installation, replace the entire `custom_components/tuya_ble_lock` folder, then restart.

Keep the existing gateway entry; its settings, saved codes, and entity IDs are retained. Adding locks uses [Reconfigure](#add-more-locks-to-an-existing-gateway), not an integration update.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Tuya BLE Lock is missing from **Add integration** | Restart Home Assistant and refresh the browser after installation. For manual installation, check that `manifest.json` is directly inside `/config/custom_components/tuya_ble_lock/`. |
| Tuya's device list is empty | Check the linked phone-app account and data center. The project must contain the gateway and the locks you want to add. |
| Cloud login or permission error | Use the project's Access ID/Secret, not your app password. Re-select the correct data center; check service authorization and trial/subscription status. |
| **No new compatible locks found**, or setup goes straight to **Manage locks** | Check that each new lock appears under this gateway in the phone app and in the cloud project. Already configured locks are omitted. A failure to read one lock's details can omit that lock; retry discovery after fixing permissions. Unsupported models are not made compatible by entering their IDs manually. |
| **This gateway is already configured** | Close the add-integration flow. Use the existing gateway's **⋮ → Reconfigure**. |
| Only **Polling interval** appears | Close **Configure** and use **⋮ → Reconfigure** to add or edit locks. |
| Gateway connection fails | Check its local IP and protocol. Home Assistant must reach it on the home network; guest Wi-Fi isolation can block this. Temporarily stop other software using the gateway locally. If it was paired again, use cloud discovery to retrieve its new key. |
| Lock is offline | Check batteries, Bluetooth range, and whether it still appears under the gateway in the app. Wake it if the model sleeps. |
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

For a new gateway, choose **Add integration → Tuya BLE Lock → Enter local gateway details manually**. For an existing gateway, start from **⋮ → Reconfigure**. Enter or retain the gateway details:

| Value | Meaning |
| --- | --- |
| Gateway IPv4 address | The gateway's private address on your home network |
| Gateway device ID | The gateway's individual Tuya identifier |
| Gateway Local Key | Its exact 16-byte connection key, including punctuation; blank retains an already saved key |
| LAN protocol version | `3.5` for the verified gateway, or the observed `3.3`/`3.4`/`3.5` for another model |

After the gateway check, choose **Add a lock manually**. Enter its name, **Lock device ID**, and **Exact child node_id**. The last value is the lock's identifier inside that gateway; it is different from the Device ID and must be copied exactly, not guessed from a Bluetooth MAC address. Complete [the lock form](#fill-in-the-lock-form), repeat for other locks, then choose **Save**.

Missing values can be read through the Tuya project's **API Explorer**: gateway device details include `local_key`; gateway sub-device queries provide each child's Device ID and `node_id`. See [Tuya's device-information API](https://developer.tuya.com/en/docs/cloud/7d3f13ae55?id=Kb2rzcwpmvaci). A cloud-reported `ip` may be a public address, so obtain the local address from your router.

### Compatibility details

The supported unlock format is **HX201-style DP61**, named `remote_no_dp_key`, with an existing eight-digit remote code. Discovery looks for that command in Tuya's device information; a Tuya logo or a similar product name alone does not establish support. See the [command-format reference](docs/remote-unlock-code.md) and [hardware-support guidance](CONTRIBUTING.md).

The known T1 / HX201 profile reports state through **DP40**. Other compatible locks use **Standard lock state (DP47)** if they report their motor state that way. Manual setup defaults to DP47, so select DP40 explicitly for the supported T1. LAN protocols 3.3 and 3.4 are configurable but have not been verified on hardware with this integration.

The integration does not pair hardware or create lock users, codes, fingerprints, or cards. All of that remains in the manufacturer's app.

## Privacy and security

Gateway keys and lock codes are saved in Home Assistant's configuration. Protect access to the server and its backups. Anyone allowed to run a lock's unlock action can use its saved code, including through automations or exposed voice assistants.

Tuya cloud credentials are used only during discovery and are not saved. The gateway and phone app may still contact Tuya independently; this integration does not disable their internet connections.

## License and development

[MIT](LICENSE). For development and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).
