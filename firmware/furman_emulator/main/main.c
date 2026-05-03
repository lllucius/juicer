/*
 * main.c — Furman F1500-UPS E serial-protocol emulator (ESP-IDF v6.0)
 *
 * Emulates the complete RS-232 command/response protocol of the Furman
 * F1500-UPS E UPS so that the Juicer CLI, GUI, and service can be tested
 * without real hardware.
 *
 * Connection
 * ----------
 * Plug the ESP32 into a USB port.  The on-board USB-UART bridge (CP2102,
 * CH340, etc.) exposes UART0 (GPIO1/TX, GPIO3/RX) as a virtual COM port:
 *
 *   Windows : COMx  (check Device Manager)
 *   Linux   : /dev/ttyUSB0  or  /dev/ttyACM0
 *   macOS   : /dev/cu.usbserial-*  or  /dev/cu.usbmodem*
 *
 * Point Juicer at that port; it will work just like the real UPS.
 *
 * Serial settings used by Juicer: 9600 baud, 8-N-1.
 *
 * Build & flash
 * -------------
 *   . $IDF_PATH/export.sh          # activate ESP-IDF v6.0 environment
 *   cd firmware/furman_emulator
 *   idf.py build
 *   idf.py -p /dev/ttyUSB0 flash
 *
 * Note: idf.py monitor will show no output because CONFIG_ESP_CONSOLE_NONE=y
 * redirects all IDF log output to /dev/null so UART0 stays clean.
 *
 * Supported commands
 * ------------------
 * Action (!):
 *   ALL_ON, ALL_OFF, SWITCH, SET_BATTHRESH, SET_BUZZER, SET_AVR,
 *   SET_FEEDBACK, SET_LINEFEED, SET_BRIGHT, SET_SCROLLMODE,
 *   SET_SLEEPMODE, RESET_ALL, SET_NORMALVOLT
 *
 * Query (?):
 *   ID, OUTLETSTAT, POWERSTAT, POWER, CURRENT, VOLTAGE, LOADSTAT,
 *   BATTERYSTAT, LIST_CONFIG, HELP
 */

#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "soc/soc_caps.h"

#if SOC_USB_SERIAL_JTAG_SUPPORTED
#include "driver/usb_serial_jtag.h"
#endif

/* ── Hardware configuration ──────────────────────────────────────────────── */

/*
 * UART0 is connected to the USB-UART bridge on classic ESP32 dev boards.
 * UART_PIN_NO_CHANGE keeps the default mapping (GPIO1/TX, GPIO3/RX).
 */
#define UART_PORT      UART_NUM_0
#define UART_BAUD      9600
#define UART_RX_BUFSZ  512  /* ring-buffer size for the UART driver */

#if SOC_USB_SERIAL_JTAG_SUPPORTED
#define USB_RX_BUFSZ   512
#define USB_TX_BUFSZ   512
#define USB_IO_TIMEOUT_MS 10
#endif

/*
 * GPIO2 carries the built-in LED on most ESP32 DevKit boards.
 * ESP32-S3 DevKit boards commonly use GPIO48 instead.
 */
#if CONFIG_IDF_TARGET_ESP32S3
#define LED_GPIO       GPIO_NUM_48
#else
#define LED_GPIO       GPIO_NUM_2
#endif

/* ── Index helpers ───────────────────────────────────────────────────────── */

/* Translate a 1-based bank number (1..4) to a 0-based array index. */
#define BANK_IDX(n)     ((n) - 1)

/*
 * Translate a battery-threshold bank number (3 or 4) to a 0-based index
 * into the two-element s_bthresh[] array.
 */
#define BTHRESH_IDX(n)  ((n) - 3)

/* ── Emulator state ──────────────────────────────────────────────────────── */

static bool s_bank[4]    = { false, false, false, false }; /* 0-based [0..3] = banks 1..4 */
static int  s_bthresh[2] = { 20, 20 };                     /* [0]=bank3, [1]=bank4 */

static bool s_buzzer     = true;
static int  s_avr_mode   = 0;    /* 0=OFF  1=STANDARD  2=SENSITIVE */
static bool s_feedback   = true;
static bool s_linefeed   = false;
static int  s_brightness = 100;  /* valid: 100, 75, 50, 25 */
static int  s_scroll     = 0;    /* 0=5SEC  1=10SEC  2=OFF */
static int  s_sleep      = 2;    /* 0=30SEC  1=60SEC  2=OFF */
static int  s_normalvolt = 230;  /* 220, 230, or 240 */

/* Simulated sensor readings — edit and re-flash to test different conditions */
static float s_volts_in  = 230.0f;
static float s_volts_out = 230.0f;
static float s_watts     = 150.0f;
static float s_current   = 0.65f;
static float s_voltage   = 230.0f;
static float s_load      = 10.0f;
static int   s_battery   = 85;
static int   s_backup_time = 60; /* minutes of backup remaining */

typedef enum {
    BATTSTATE_FULL       = 0,
    BATTSTATE_CHARGE     = 1,
    BATTSTATE_DISCHARGE  = 2,
} battstate_t;

static battstate_t s_battstate = BATTSTATE_FULL;

/* ── Output helpers ──────────────────────────────────────────────────────── */

typedef enum {
    REPLY_TRANSPORT_UART = 0,
#if SOC_USB_SERIAL_JTAG_SUPPORTED
    REPLY_TRANSPORT_USB,
#endif
} reply_transport_t;

static reply_transport_t s_reply_transport = REPLY_TRANSPORT_UART;

static void transport_write(const char *data, size_t len)
{
#if SOC_USB_SERIAL_JTAG_SUPPORTED
    if (s_reply_transport == REPLY_TRANSPORT_USB) {
        usb_serial_jtag_write_bytes((const uint8_t *)data, len, USB_IO_TIMEOUT_MS);
        return;
    }
#endif
    uart_write_bytes(UART_PORT, data, len);
}

static int transport_read_byte(uint8_t *byte, uint32_t timeout_ms)
{
    int n;

#if SOC_USB_SERIAL_JTAG_SUPPORTED
    n = usb_serial_jtag_read_bytes(byte, 1, timeout_ms);
    if (n > 0) {
        s_reply_transport = REPLY_TRANSPORT_USB;
        return n;
    }
#endif
    n = uart_read_bytes(UART_PORT, byte, 1, pdMS_TO_TICKS(timeout_ms));
    if (n > 0) {
        s_reply_transport = REPLY_TRANSPORT_UART;
    }
    return n;
}

/*
 * Send one response line followed by CR (and LF when LINEFEED mode is ON).
 * All Furman responses are CR-terminated; the host strips the CR when reading.
 */
static void sendln(const char *s)
{
    transport_write(s, strlen(s));
    transport_write("\r", 1);
    if (s_linefeed) {
        transport_write("\n", 1);
    }
}

static void invalid_param(void)
{
    sendln("$INVALID_PARAMETER");
}

/* ── Bank helpers ────────────────────────────────────────────────────────── */

static void send_bank(int n)
{
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BANK %d = %s", n, s_bank[BANK_IDX(n)] ? "ON" : "OFF");
    sendln(tmp);
}

static void send_all_banks(void)
{
    for (int i = 1; i <= 4; i++) {
        send_bank(i);
    }
}

/* ── Command matching helpers ────────────────────────────────────────────── */

/*
 * Return true when 'input' exactly equals 'name' (no trailing characters).
 */
static bool cmd_exact(const char *input, const char *name)
{
    return strcmp(input, name) == 0;
}

/*
 * Return true when 'input' starts with 'prefix' followed by a single space.
 * Sets *args to the first character after that space.
 */
static bool cmd_prefix(const char *input, const char *prefix, const char **args)
{
    size_t n = strlen(prefix);
    if (strncmp(input, prefix, n) == 0 && input[n] == ' ') {
        *args = input + n + 1;
        return true;
    }
    return false;
}

/* ── Action command handlers ─────────────────────────────────────────────── */

static void handle_all_on(void)
{
    for (int i = 0; i < 4; i++) {
        s_bank[i] = true;
    }
    send_all_banks();
}

static void handle_all_off(void)
{
    for (int i = 0; i < 4; i++) {
        s_bank[i] = false;
    }
    send_all_banks();
}

/* "SWITCH <bank> <ON|OFF>" */
static void handle_switch(const char *args)
{
    int  b;
    char state[8];
    if (sscanf(args, "%d %7s", &b, state) != 2 || b < 1 || b > 4) {
        invalid_param();
        return;
    }
    if (strcmp(state, "ON") == 0) {
        s_bank[BANK_IDX(b)] = true;
    } else if (strcmp(state, "OFF") == 0) {
        s_bank[BANK_IDX(b)] = false;
    } else {
        invalid_param();
        return;
    }
    send_bank(b);
}

/* "SET_BATTHRESH <bank> <level>" — bank must be 3 or 4, level 20-100 */
static void handle_set_batthresh(const char *args)
{
    int b, level;
    if (sscanf(args, "%d %d", &b, &level) != 2 ||
        (b != 3 && b != 4) ||
        level < 20 || level > 100)
    {
        invalid_param();
        return;
    }
    /* The real device rounds up to the nearest 10. */
    level = ((level + 9) / 10) * 10;
    s_bthresh[BTHRESH_IDX(b)] = level;
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$BTHRESH %d = %d", b, s_bthresh[BTHRESH_IDX(b)]);
    sendln(tmp);
}

/* "SET_BUZZER <ON|OFF>" */
static void handle_set_buzzer(const char *args)
{
    if (strcmp(args, "ON") == 0) {
        s_buzzer = true;
    } else if (strcmp(args, "OFF") == 0) {
        s_buzzer = false;
    } else {
        invalid_param();
        return;
    }
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BUZZER = %s", s_buzzer ? "ON" : "OFF");
    sendln(tmp);
}

/* "SET_AVR <OFF|STANDARD|SENSITIVE>" */
static void handle_set_avr(const char *args)
{
    static const char *modes[] = { "OFF", "STANDARD", "SENSITIVE" };
    if (strcmp(args, "OFF") == 0)            s_avr_mode = 0;
    else if (strcmp(args, "STANDARD") == 0)  s_avr_mode = 1;
    else if (strcmp(args, "SENSITIVE") == 0) s_avr_mode = 2;
    else { invalid_param(); return; }
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$AVR = %s", modes[s_avr_mode]);
    sendln(tmp);
}

/* "SET_FEEDBACK <ON|OFF>" */
static void handle_set_feedback(const char *args)
{
    if (strcmp(args, "ON") == 0)       s_feedback = true;
    else if (strcmp(args, "OFF") == 0) s_feedback = false;
    else { invalid_param(); return; }
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$FEEDBACK = %s", s_feedback ? "ON" : "OFF");
    sendln(tmp);
}

/* "SET_LINEFEED <ON|OFF>" */
static void handle_set_linefeed(const char *args)
{
    if (strcmp(args, "ON") == 0)       s_linefeed = true;
    else if (strcmp(args, "OFF") == 0) s_linefeed = false;
    else { invalid_param(); return; }
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$LINEFEED = %s", s_linefeed ? "ON" : "OFF");
    sendln(tmp);
}

/*
 * "SET_BRIGHT <100|075|050|025>"
 * The protocol uses zero-padded 3-digit strings; atoi() handles "075" as 75.
 */
static void handle_set_bright(const char *args)
{
    static const int valid[] = { 100, 75, 50, 25 };
    int b = atoi(args);
    bool ok = false;
    for (int i = 0; i < 4; i++) {
        if (b == valid[i]) { ok = true; break; }
    }
    if (!ok) { invalid_param(); return; }
    s_brightness = b;
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BRIGHTNESS = %03d", s_brightness);
    sendln(tmp);
}

/* "SET_SCROLLMODE <5SEC|10SEC|OFF>" */
static void handle_set_scrollmode(const char *args)
{
    static const char *modes[] = { "5SEC", "10SEC", "OFF" };
    if (strcmp(args, "5SEC") == 0)       s_scroll = 0;
    else if (strcmp(args, "10SEC") == 0) s_scroll = 1;
    else if (strcmp(args, "OFF") == 0)   s_scroll = 2;
    else { invalid_param(); return; }
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$SCROLL_MODE = %s", modes[s_scroll]);
    sendln(tmp);
}

/* "SET_SLEEPMODE <30SEC|60SEC|OFF>" */
static void handle_set_sleepmode(const char *args)
{
    static const char *modes[] = { "30SEC", "60SEC", "OFF" };
    if (strcmp(args, "30SEC") == 0)      s_sleep = 0;
    else if (strcmp(args, "60SEC") == 0) s_sleep = 1;
    else if (strcmp(args, "OFF") == 0)   s_sleep = 2;
    else { invalid_param(); return; }
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$SLEEP_MODE = %s", modes[s_sleep]);
    sendln(tmp);
}

/* "RESET_ALL" — restore all settings to factory defaults */
static void handle_reset_all(void)
{
    for (int i = 0; i < 4; i++) { s_bank[i] = false; }
    s_bthresh[0] = s_bthresh[1] = 20;
    s_buzzer     = true;
    s_avr_mode   = 0;
    s_feedback   = true;
    s_linefeed   = false;
    s_brightness = 100;
    s_scroll     = 0;
    s_sleep      = 2;
    s_normalvolt = 230;
    sendln("$FACTORY SETTINGS RESTORED");
}

/* "SET_NORMALVOLT <220|230|240>" */
static void handle_set_normalvolt(const char *args)
{
    int v = atoi(args);
    if (v != 220 && v != 230 && v != 240) { invalid_param(); return; }
    s_normalvolt = v;
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$NORMALVOLT = %d", s_normalvolt);
    sendln(tmp);
}

/* ── Query handlers ───────────────────────────────────────────────────────── */

static void handle_query_id(void)
{
    sendln("$Furman");
    sendln("$F1500-UPS E");
    sendln("$FW1.00 (Emulator)");
}

static void handle_query_outletstat(void)
{
    send_all_banks();
}

static void handle_query_powerstat(void)
{
    sendln("$PWR = NORMAL");
}

static void handle_query_power(void)
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$VOLTS_IN = %.1f",  s_volts_in);  sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$VOLTS_OUT = %.1f", s_volts_out); sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$WATTS = %.1f",     s_watts);     sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$CURRENT = %.2f",   s_current);   sendln(tmp);
}

static void handle_query_current(void)
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$CURRENT = %.2f", s_current);
    sendln(tmp);
}

static void handle_query_voltage(void)
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$VOLTAGE = %.1f", s_voltage);
    sendln(tmp);
}

static void handle_query_loadstat(void)
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$LOAD = %.1f", s_load);
    sendln(tmp);
}

static void handle_query_batterystat(void)
{
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BATTERY = %d", s_battery);
    sendln(tmp);
}

static void handle_query_battstate(void)
{
    static const char *states[] = {
        [BATTSTATE_FULL]      = "FULL",
        [BATTSTATE_CHARGE]    = "CHARGE",
        [BATTSTATE_DISCHARGE] = "DISCHARGE",
    };
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$BATTSTATE = %s", states[s_battstate]);
    sendln(tmp);
}

static void handle_query_time(void)
{
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$TIME = %d", s_backup_time);
    sendln(tmp);
}

static void handle_query_list_config(void)
{
    static const char *avr_modes[]    = { "OFF", "STANDARD", "SENSITIVE" };
    static const char *scroll_modes[] = { "5SEC", "10SEC", "OFF" };
    static const char *sleep_modes[]  = { "30SEC", "60SEC", "OFF" };
    char tmp[48];

    snprintf(tmp, sizeof(tmp), "$BUZZER = %s",       s_buzzer ? "ON" : "OFF");       sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$AVR = %s",           avr_modes[s_avr_mode]);         sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$FEEDBACK = %s",      s_feedback ? "ON" : "OFF");     sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$LINEFEED = %s",      s_linefeed ? "ON" : "OFF");     sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$BRIGHTNESS = %03d",  s_brightness);                  sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$SCROLL_MODE = %s",   scroll_modes[s_scroll]);        sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$SLEEP_MODE = %s",    sleep_modes[s_sleep]);          sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$NORMALVOLT = %d",    s_normalvolt);                  sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$BTHRESH 3 = %d",     s_bthresh[BTHRESH_IDX(3)]);    sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$BTHRESH 4 = %d",     s_bthresh[BTHRESH_IDX(4)]);    sendln(tmp);
}

static void handle_query_help(void)
{
    static const char *cmds[] = {
        "!ALL_ON",
        "!ALL_OFF",
        "!SWITCH <bank> <ON|OFF>",
        "!SET_BATTHRESH <bank> <level>",
        "!SET_BUZZER <ON|OFF>",
        "!SET_AVR <OFF|STANDARD|SENSITIVE>",
        "!SET_FEEDBACK <ON|OFF>",
        "!SET_LINEFEED <ON|OFF>",
        "!SET_BRIGHT <100|075|050|025>",
        "!SET_SCROLLMODE <5SEC|10SEC|OFF>",
        "!SET_SLEEPMODE <30SEC|60SEC|OFF>",
        "!RESET_ALL",
        "!SET_NORMALVOLT <220|230|240>",
        "?ID",
        "?OUTLETSTAT",
        "?POWERSTAT",
        "?POWER",
        "?CURRENT",
        "?VOLTAGE",
        "?LOADSTAT",
        "?BATTERYSTAT",
        "?BATTSTATE",
        "?TIME",
        "?LIST_CONFIG",
        "?HELP",
        NULL
    };
    for (int i = 0; cmds[i] != NULL; i++) {
        sendln(cmds[i]);
    }
}

/* ── Main command dispatcher ──────────────────────────────────────────────── */

static void dispatch(const char *cmd)
{
    /* Skip any leading whitespace (defensive; the protocol doesn't send it). */
    while (*cmd == ' ' || *cmd == '\t') { cmd++; }
    if (*cmd == '\0') { return; }

    const char *args = NULL;

    if (cmd[0] == '!') {
        const char *c = cmd + 1;

        if (cmd_exact(c, "ALL_ON"))                        { handle_all_on();             return; }
        if (cmd_exact(c, "ALL_OFF"))                       { handle_all_off();            return; }
        if (cmd_prefix(c, "SWITCH", &args))                { handle_switch(args);         return; }
        if (cmd_prefix(c, "SET_BATTHRESH", &args))         { handle_set_batthresh(args);  return; }
        if (cmd_prefix(c, "SET_BUZZER", &args))            { handle_set_buzzer(args);     return; }
        if (cmd_prefix(c, "SET_AVR", &args))               { handle_set_avr(args);        return; }
        if (cmd_prefix(c, "SET_FEEDBACK", &args))          { handle_set_feedback(args);   return; }
        if (cmd_prefix(c, "SET_LINEFEED", &args))          { handle_set_linefeed(args);   return; }
        if (cmd_prefix(c, "SET_BRIGHT", &args))            { handle_set_bright(args);     return; }
        if (cmd_prefix(c, "SET_SCROLLMODE", &args))        { handle_set_scrollmode(args); return; }
        if (cmd_prefix(c, "SET_SLEEPMODE", &args))         { handle_set_sleepmode(args);  return; }
        if (cmd_exact(c, "RESET_ALL"))                     { handle_reset_all();          return; }
        if (cmd_prefix(c, "SET_NORMALVOLT", &args))        { handle_set_normalvolt(args); return; }

    } else if (cmd[0] == '?') {
        const char *c = cmd + 1;

        if (cmd_exact(c, "ID"))          { handle_query_id();          return; }
        if (cmd_exact(c, "OUTLETSTAT"))  { handle_query_outletstat();  return; }
        if (cmd_exact(c, "POWERSTAT"))   { handle_query_powerstat();   return; }
        if (cmd_exact(c, "POWER"))       { handle_query_power();       return; }
        if (cmd_exact(c, "CURRENT"))     { handle_query_current();     return; }
        if (cmd_exact(c, "VOLTAGE"))     { handle_query_voltage();     return; }
        if (cmd_exact(c, "LOADSTAT"))    { handle_query_loadstat();    return; }
        if (cmd_exact(c, "BATTERYSTAT")) { handle_query_batterystat(); return; }
        if (cmd_exact(c, "BATTSTATE"))   { handle_query_battstate();   return; }
        if (cmd_exact(c, "TIME"))        { handle_query_time();        return; }
        if (cmd_exact(c, "LIST_CONFIG")) { handle_query_list_config(); return; }
        if (cmd_exact(c, "HELP"))        { handle_query_help();        return; }
    }

    invalid_param();
}

/* ── Application entry point ─────────────────────────────────────────────── */

void app_main(void)
{
    /* Configure UART0 for 9600/8-N-1, matching Juicer's SerialTransport. */
    const uart_config_t uart_cfg = {
        .baud_rate  = UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    uart_param_config(UART_PORT, &uart_cfg);
    uart_set_pin(UART_PORT,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    /*
     * Install driver with a receive ring buffer and no TX buffer
     * (blocking writes).  No event queue needed for this application.
     */
    uart_driver_install(UART_PORT, UART_RX_BUFSZ, 0, 0, NULL, 0);

#if SOC_USB_SERIAL_JTAG_SUPPORTED
    usb_serial_jtag_driver_config_t usb_cfg = {
        .tx_buffer_size = USB_TX_BUFSZ,
        .rx_buffer_size = USB_RX_BUFSZ,
    };
    usb_serial_jtag_driver_install(&usb_cfg);
#endif

    /* Blink the built-in LED once to signal that the emulator is ready. */
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(LED_GPIO, 1);
    vTaskDelay(pdMS_TO_TICKS(200));
    gpio_set_level(LED_GPIO, 0);

    /* Main protocol loop: accumulate bytes into cmd_buf; dispatch on CR. */
    char    cmd_buf[128];
    int     cmd_len = 0;
    uint8_t byte;

    for (;;) {
        int n = transport_read_byte(&byte, 10);
        if (n <= 0) {
            continue;
        }

        char c = (char)byte;

        if (c == '\r') {
            /* CR marks the end of a Furman protocol command. */
            cmd_buf[cmd_len] = '\0';
            if (cmd_len > 0) {
                dispatch(cmd_buf);
            }
            cmd_len = 0;
        } else if (c == '\n') {
            /* Ignore LF — some hosts send CRLF; the real device uses CR only. */
        } else if (cmd_len < (int)(sizeof(cmd_buf) - 1)) {
            cmd_buf[cmd_len++] = c;
        }
        /*
         * If the buffer overflows the next CR will dispatch a truncated
         * command, which will fall through to invalid_param().
         */
    }
}
