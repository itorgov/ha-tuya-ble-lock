# Advanced: enter unlock information manually

For supported T1 / HX201 locks, use [automatic setup](../README.md#add-the-gateway-in-home-assistant) first. Home Assistant normally imports the credential for you. This guide is for **Advanced manual setup**, other compatible models, or changing an existing lock's saved credential.

You will copy a successful app-unlock command from Tuya into Home Assistant. The integration extracts the existing remote unlock code; you do not need to decode it yourself or create a new code.

Before starting, [set up the devices in the phone app](../README.md#set-up-the-devices-in-the-phone-app) and [prepare Tuya cloud access](../README.md#prepare-tuya-cloud-access). The lock must open through its gateway in Tuya Smart or Smart Life, and appear in your linked cloud project. You do not need to have added it to Home Assistant yet.

Do this separately for each lock. The command contains a working credential: keep it private, and do not put it in screenshots, issues, or online decoding tools.

## 1. Make one successful app unlock

1. Stand beside the lock and turn off **Bluetooth on your phone**. This makes the app use the gateway instead of connecting directly to the lock.
2. Switch the phone to **mobile data** by turning off its Wi-Fi. Keep the gateway powered and connected to Wi-Fi. Tuya notes that app commands sent over the same local network can be absent from cloud logs. [Tuya's logging guidance](https://support.tuya.com/en/help/_detail/Ka3kirkwu0w16).
3. Unlock once in Tuya Smart or Smart Life. Confirm that the lock opens, and note the date and time. If unlocking fails, resolve that in the app before continuing.

## 2. Find the command in Device Logs

1. Sign in to the [Tuya Developer Platform](https://platform.tuya.com/). Open **Cloud → Development**, select your linked project, then open **Devices → All Devices**. Select the matching data center and **Tuya App's Account** filter if shown.
2. Find the **lock**, not the gateway. Copy its **Device ID** for the fallback method below, then click **Debug Device** in that lock's row.
3. Open **Device Logs**. Stay on the logs page; you do not need **Send Instruction**. [Tuya's device guide](https://developer.tuya.com/en/docs/iot/Management-debug-device?id=Kar2zdgghi2vu).
4. Select a short time range around your unlock, such as two minutes before it through the current time. Check the portal's time zone, then search or refresh. If available, filter by **DP ID 61** or **`remote_no_dp_key`**. Tuya documents filters for data point, event, and time; labels and controls can vary between accounts. [Device log filters](https://developer.tuya.com/en/docs/iot/debug?id=Kat5sq6hd1hjt).
5. Find the **command sent to the lock** at that time, often labeled an instruction/command event. Open its details, if available, and find the raw **value** for `remote_no_dp_key` / DP61.
6. Copy only its **Base64 value**, without surrounding quotation marks, spaces, or the rest of the JSON. Base64 is simply the command represented as text; it is not the eight-digit code itself.

Once copied, [paste it into Home Assistant](#4-paste-the-command-into-home-assistant). If the portal hides the value or shows only status reports, try API Explorer below. Some products do not expose a usable command through either method.

## 3. Alternative: use API Explorer

API Explorer is Tuya's web form for reading logs; no script is needed.

1. Open **Cloud → API Explorer**, or your project's **Service API → Debug**. Select your linked project and its data center. [Tuya's API Explorer entry points](https://developer.tuya.com/en/docs/im-project-services/Cloud-self-development?id=Kb3o4z4ll4v9d).
2. Select **IoT Core → Device Management → Get Operation Log**. Check that the displayed operation is **`GET /v2.0/cloud/thing/{device_id}/logs`**. If unavailable, check your project's IoT Core subscription and authorization.
3. Fill in these fields:

   | Field | What to enter |
   | --- | --- |
   | `device_id` | The **lock's** Device ID copied above. |
   | `type` | `5` — commands sent to the lock. |
   | `codes` | `remote_no_dp_key`. If this returns nothing, try leaving it empty. |
   | `start_time` | Two minutes before the unlock, in milliseconds; see below. |
   | `end_time` | A time after the unlock, such as the current time, in milliseconds. |
   | `size` | `100`. |
   | `query_type` | `1`, for the free-edition query. Access depends on your account. |
   | `start_row_key` | Leave empty for the first request. |

   Leave `last_row_key` and `last_event_time` empty if shown. See [Tuya's Get Operation Log reference](https://developer.tuya.com/en/docs/cloud/bdc1e6f858?id=Kcp2l3tqdozts).

### Get the two time values

The API expects a **13-digit number**, not a date written as text. Use the form's date/time picker if provided. Otherwise:

1. Open [Epoch Converter](https://www.epochconverter.com/). Enter only dates and times there, never the command or device credentials.
2. Enter the date and time two minutes before your unlock. Set the converter's time zone to match the time you noted, then choose **Date to timestamp**.
3. Copy the **milliseconds** result into `start_time`. If it shows only a 10-digit seconds result, append **three zeroes** to make milliseconds.
4. Repeat for a time after the unlock and paste it into `end_time`. The end value must be larger than the start value.

### Read the result

Run the query using the request button. In `result.logs`, find the entry at your successful unlock time with `code` = `remote_no_dp_key`, `event_id` = `5`, and `status` = `"1"`. Copy **only the Base64 text inside that entry's `value` field**, without quotation marks or surrounding JSON. A device report (event type `7`) may contain only an acknowledgement and is not the command you need.

If `has_next` is `true`, shorten the time range around your single app unlock and query again. Keep the code filter and `size` = `100`. This avoids choosing a command from an incomplete set of results.

## 4. Paste the command into Home Assistant

### For a new lock

- **First gateway:** choose **Add integration → Tuya BLE Lock → Advanced manual setup**. Follow [Manual gateway and lock entry](../README.md#manual-gateway-and-lock-entry), then choose **Add a lock manually** and identify the lock whose command you copied.
- **Another lock on an existing gateway:** open the gateway's **⋮ → Reconfigure → Advanced manual setup**. Keep the saved gateway details and leave the Local Key blank. After the gateway check, choose **Add a lock manually** and enter the new lock's identifiers.

Both routes lead to **Configure lock status and unlock code**. If an automatic setup is still open, close it before starting the manual route; unfinished changes are discarded. A lock already added successfully through automatic setup does not need its command pasted again.

### For a lock already in Home Assistant

Open **Settings → Devices & services → Tuya BLE Lock**, open the gateway entry's menu, and choose **Reconfigure → Advanced manual setup**. Keep the saved gateway settings and leave its local-key field blank. After the gateway check, choose **Edit a lock or its code**, then select this lock. No cloud credentials are needed here. See [Change a lock's settings](../README.md#change-a-locks-settings).

### Fill in the lock form

1. Confirm **My lock uses the supported DP61 profile** and choose the lock state source described in [Fill in the lock form](../README.md#fill-in-the-lock-form).
2. Paste the value into **Existing DP61 command (Base64)**. Leave **Existing eight-digit remote unlock code** empty and **Remove the saved unlock code** unchecked.
3. Leave **Member number** at its displayed value. The command supplies the correct number, which identifies the lock's existing remote-unlock credential; it is not a Home Assistant user number.
4. Submit the form. Add any other locks with their own commands, then choose **Save** in **Manage locks**.

The integration checks the command locally and saves the extracted member number and eight-digit code. Saving does not unlock the device. You can then test **Lock: Unlock** while beside the lock, following the README.

## If something goes wrong

| Problem | What to check |
| --- | --- |
| Lock missing from the project | Correct app account and data center; account linked by QR code; lock included in the linked devices. See [Tuya Cloud setup](../README.md#prepare-tuya-cloud-access). |
| Permission error or API unavailable | In the project's **Service API**, check that IoT Core is authorized and its subscription has not expired. Check the selected project, linked app account, and data center too. |
| No matching logs | Use the lock's Device ID, not the gateway ID or Product ID. Check the time zone and time range, refresh after logs arrive, and remove the code filter if necessary. A direct phone-Bluetooth unlock is not a gateway command. |
| Only a short acknowledgement | Find the outgoing command, event type `5`. The supported command is 13 decoded bytes; a three-byte response does not contain the credential. |
| Home Assistant rejects the value | Copy the complete Base64 value without JSON, quotes, or whitespace. A hexadecimal display is not Base64. This field cannot directly import hex or a different command format. |
| Saved code stops working | Some locks replace the remote code after app activity. Obtain the current command for this same lock again; do not guess a replacement code. |

If no supported command is available, this integration cannot recover or create the credential. When requesting help, share the model and error message, not the command or raw logs.

## Advanced: supported command format

DP61 is the Tuya data point used for this profile's remote-unlock command, named `remote_no_dp_key`. The Base64 value must decode to exactly **13 bytes**:

```text
Byte offset    Length    Meaning
0              1         0x01
1              2         Member number, unsigned big-endian
3              8         Existing remote unlock code, ASCII digits
11             2         Source: 0x00 0x00 (local) or 0x00 0x01 (app)
```

Import accepts exactly those two source values. Local unlock commands use `0x00 0x00`; see the [published source-byte observations](https://github.com/make-all/tuya-local/issues/6248#issuecomment-5971747722). The member number must be 1–65534. Preserve leading zeroes in the eight-digit code. The gateway local key, app password, and keypad PIN are separate credentials. DP69/DP71/DP73 values can identify users and are not substitutes for this command.

See the [published protocol evidence](https://github.com/make-all/tuya-local/issues/6248). This format is not shared by every Tuya lock. If you inspect or convert the bytes yourself, do so locally; never upload this value to an online decoder.
