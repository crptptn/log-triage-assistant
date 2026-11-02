# USB-устройства под Wine: устройство не открывается

**Симптомы.** Печать или сканирование падает, в device-agent ошибки libusb (LIBUSB_ERROR_NO_DEVICE, LIBUSB_ERROR_ACCESS), «Permission denied» на /dev/bus/usb/…, шлюз отвечает 502 на /api/v1/print.

**Почему так бывает.** После переподключения или перезагрузки устройство получает новый номер на шине. Если udev-правило выдаёт права по номеру устройства, а не по VID/PID, процесс под Wine теряет доступ.

**Как подтвердить.** `lsusb` — устройство видно; `ls -l /dev/bus/usb/<bus>/<dev>` — права у root; в логе агента ошибка открытия сразу после реконнекта.

**Что сделать.** Правило udev по ATTRS{idVendor}/ATTRS{idProduct} с GROUP и MODE 0660, `udevadm control --reload && udevadm trigger`; агент — с повторным открытием устройства после события add.
