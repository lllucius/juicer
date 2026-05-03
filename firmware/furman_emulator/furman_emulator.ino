/*
 * furman_emulator.ino
 *
 * Furman F1500-UPS E serial-protocol emulator for ESP32.
 *
 * Emulates the complete RS-232 command/response protocol of the Furman
 * F1500-UPS E UPS so that the Juicer CLI, GUI, and service can be tested
 * without real hardware.
 *
 * Connection
 * ----------
 * Plug the ESP32 into a USB port.  The host OS will enumerate it as a
 * virtual COM port (COMx on Windows, /dev/ttyUSBx or /dev/ttyACMx on
 * Linux/macOS).  Point Juicer at that port; it will work just like the
 * real UPS.
 *
 * Serial settings used by Juicer: 9600 baud, 8-N-1.
 * USB-CDC adapters ignore the host baud-rate setting, so any speed works
 * transparently over USB.  When wiring to a hardware UART (e.g. for an
 * RS-232 level-shifter), make sure both sides are set to 9600 baud.
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

#include <Arduino.h>

// ── Emulator state ────────────────────────────────────────────────────────────

static bool bank[5]    = { false, false, false, false, false }; // 1-indexed [1..4]
static int  bthresh[5] = { 0, 0, 0, 20, 20 };                  // banks 3 & 4 only

static bool buzzer     = true;
static int  avr_mode   = 0;    // 0=OFF  1=STANDARD  2=SENSITIVE
static bool feedback   = true;
static bool linefeed   = false;
static int  brightness = 100;  // valid: 100, 75, 50, 25
static int  scroll     = 0;    // 0=5SEC  1=10SEC  2=OFF
static int  sleep_m    = 2;    // 0=30SEC  1=60SEC  2=OFF
static int  normalvolt = 230;  // 220, 230, or 240

// Simulated sensor readings
static float s_volts_in  = 230.0f;
static float s_volts_out = 230.0f;
static float s_watts     = 150.0f;
static float s_current   = 0.65f;
static float s_voltage   = 230.0f;
static float s_load      = 10.0f;
static int   s_battery   = 85;
static int   s_backup_time = 30;  // minutes of backup remaining

// ── Command input buffer ──────────────────────────────────────────────────────

static char cmd_buf[128];
static int  cmd_len = 0;

// ── Output helpers ────────────────────────────────────────────────────────────

/*
 * Send one response line followed by CR (and LF when LINEFEED mode is ON).
 * All Furman responses are CR-terminated; the host strips the CR when reading.
 */
static void sendln(const char *s)
{
    Serial.print(s);
    Serial.write('\r');
    if (linefeed) {
        Serial.write('\n');
    }
}

static void sendln(const String &s)
{
    sendln(s.c_str());
}

static void invalid_param()
{
    sendln("$INVALID_PARAMETER");
}

// ── Bank helpers ──────────────────────────────────────────────────────────────

static void send_bank(int n)
{
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BANK %d = %s", n, bank[n] ? "ON" : "OFF");
    sendln(tmp);
}

static void send_all_banks()
{
    for (int i = 1; i <= 4; i++) {
        send_bank(i);
    }
}

// ── Command matching helpers ──────────────────────────────────────────────────

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

// ── Action command handlers ───────────────────────────────────────────────────

static void handle_all_on()
{
    for (int i = 1; i <= 4; i++) {
        bank[i] = true;
    }
    send_all_banks();
}

static void handle_all_off()
{
    for (int i = 1; i <= 4; i++) {
        bank[i] = false;
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
        bank[b] = true;
    } else if (strcmp(state, "OFF") == 0) {
        bank[b] = false;
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
    // The real device rounds up to the nearest 10.
    level = ((level + 9) / 10) * 10;
    bthresh[b] = level;
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$BTHRESH %d = %d", b, bthresh[b]);
    sendln(tmp);
}

/* "SET_BUZZER <ON|OFF>" */
static void handle_set_buzzer(const char *args)
{
    if (strcmp(args, "ON") == 0) {
        buzzer = true;
    } else if (strcmp(args, "OFF") == 0) {
        buzzer = false;
    } else {
        invalid_param();
        return;
    }
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BUZZER = %s", buzzer ? "ON" : "OFF");
    sendln(tmp);
}

/* "SET_AVR <OFF|STANDARD|SENSITIVE>" */
static void handle_set_avr(const char *args)
{
    static const char *modes[] = { "OFF", "STANDARD", "SENSITIVE" };
    if (strcmp(args, "OFF") == 0)       avr_mode = 0;
    else if (strcmp(args, "STANDARD") == 0)  avr_mode = 1;
    else if (strcmp(args, "SENSITIVE") == 0) avr_mode = 2;
    else { invalid_param(); return; }
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$AVR = %s", modes[avr_mode]);
    sendln(tmp);
}

/* "SET_FEEDBACK <ON|OFF>" */
static void handle_set_feedback(const char *args)
{
    if (strcmp(args, "ON") == 0)       feedback = true;
    else if (strcmp(args, "OFF") == 0) feedback = false;
    else { invalid_param(); return; }
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$FEEDBACK = %s", feedback ? "ON" : "OFF");
    sendln(tmp);
}

/* "SET_LINEFEED <ON|OFF>" */
static void handle_set_linefeed(const char *args)
{
    if (strcmp(args, "ON") == 0)       linefeed = true;
    else if (strcmp(args, "OFF") == 0) linefeed = false;
    else { invalid_param(); return; }
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$LINEFEED = %s", linefeed ? "ON" : "OFF");
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
    brightness = b;
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BRIGHTNESS = %03d", brightness);
    sendln(tmp);
}

/* "SET_SCROLLMODE <5SEC|10SEC|OFF>" */
static void handle_set_scrollmode(const char *args)
{
    static const char *modes[] = { "5SEC", "10SEC", "OFF" };
    if (strcmp(args, "5SEC") == 0)       scroll = 0;
    else if (strcmp(args, "10SEC") == 0) scroll = 1;
    else if (strcmp(args, "OFF") == 0)   scroll = 2;
    else { invalid_param(); return; }
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$SCROLL_MODE = %s", modes[scroll]);
    sendln(tmp);
}

/* "SET_SLEEPMODE <30SEC|60SEC|OFF>" */
static void handle_set_sleepmode(const char *args)
{
    static const char *modes[] = { "30SEC", "60SEC", "OFF" };
    if (strcmp(args, "30SEC") == 0)      sleep_m = 0;
    else if (strcmp(args, "60SEC") == 0) sleep_m = 1;
    else if (strcmp(args, "OFF") == 0)   sleep_m = 2;
    else { invalid_param(); return; }
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$SLEEP_MODE = %s", modes[sleep_m]);
    sendln(tmp);
}

/* "RESET_ALL" — restore all settings to factory defaults */
static void handle_reset_all()
{
    for (int i = 1; i <= 4; i++) { bank[i] = false; }
    bthresh[3] = bthresh[4] = 20;
    buzzer     = true;
    avr_mode   = 0;
    feedback   = true;
    linefeed   = false;
    brightness = 100;
    scroll     = 0;
    sleep_m    = 2;
    normalvolt = 230;
    sendln("$FACTORY SETTINGS RESTORED");
}

/* "SET_NORMALVOLT <220|230|240>" */
static void handle_set_normalvolt(const char *args)
{
    int v = atoi(args);
    if (v != 220 && v != 230 && v != 240) { invalid_param(); return; }
    normalvolt = v;
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$NORMALVOLT = %d", normalvolt);
    sendln(tmp);
}

// ── Query handlers ────────────────────────────────────────────────────────────

static void handle_query_id()
{
    sendln("$Furman");
    sendln("$F1500-UPS E");
    sendln("$FW1.00 (Emulator)");
}

static void handle_query_outletstat()
{
    send_all_banks();
}

static void handle_query_powerstat()
{
    sendln("$PWR = NORMAL");
}

static void handle_query_power()
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$VOLTS_IN = %.1f",  s_volts_in);  sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$VOLTS_OUT = %.1f", s_volts_out); sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$WATTS = %.1f",     s_watts);     sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$CURRENT = %.2f",   s_current);   sendln(tmp);
}

static void handle_query_current()
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$CURRENT = %.2f", s_current);
    sendln(tmp);
}

static void handle_query_voltage()
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$VOLTAGE = %.1f", s_voltage);
    sendln(tmp);
}

static void handle_query_loadstat()
{
    char tmp[32];
    snprintf(tmp, sizeof(tmp), "$LOAD = %.1f", s_load);
    sendln(tmp);
}

static void handle_query_batterystat()
{
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "$BATTERY = %d", s_battery);
    sendln(tmp);
}

static void handle_query_list_config()
{
    static const char *avr_modes[]    = { "OFF", "STANDARD", "SENSITIVE" };
    static const char *scroll_modes[] = { "5SEC", "10SEC", "OFF" };
    static const char *sleep_modes[]  = { "30SEC", "60SEC", "OFF" };
    char tmp[48];

    snprintf(tmp, sizeof(tmp), "$BUZZER = %s",          buzzer ? "ON" : "OFF"); sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$AVR = %s",             avr_modes[avr_mode]);   sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$FEEDBACK = %s",        feedback ? "ON" : "OFF"); sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$LINEFEED = %s",        linefeed ? "ON" : "OFF"); sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$BRIGHTNESS = %03d",    brightness);            sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$SCROLL_MODE = %s",     scroll_modes[scroll]);  sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$SLEEP_MODE = %s",      sleep_modes[sleep_m]);  sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$NORMALVOLT = %d",      normalvolt);            sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$BTHRESH 3 = %d",       bthresh[3]);            sendln(tmp);
    snprintf(tmp, sizeof(tmp), "$BTHRESH 4 = %d",       bthresh[4]);            sendln(tmp);
}

static void handle_query_help()
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
        "?LIST_CONFIG",
        "?HELP",
        nullptr
    };
    for (int i = 0; cmds[i] != nullptr; i++) {
        sendln(cmds[i]);
    }
}

// ── Main command dispatcher ───────────────────────────────────────────────────

static void dispatch(const char *cmd)
{
    // Skip any leading whitespace (defensive; the protocol doesn't send it).
    while (*cmd == ' ' || *cmd == '\t') { cmd++; }
    if (*cmd == '\0') { return; }

    const char *args = nullptr;

    if (cmd[0] == '!') {
        const char *c = cmd + 1;

        if (cmd_exact(c, "ALL_ON"))                          { handle_all_on();                    return; }
        if (cmd_exact(c, "ALL_OFF"))                         { handle_all_off();                   return; }
        if (cmd_prefix(c, "SWITCH", &args))                  { handle_switch(args);                return; }
        if (cmd_prefix(c, "SET_BATTHRESH", &args))           { handle_set_batthresh(args);         return; }
        if (cmd_prefix(c, "SET_BUZZER", &args))              { handle_set_buzzer(args);            return; }
        if (cmd_prefix(c, "SET_AVR", &args))                 { handle_set_avr(args);               return; }
        if (cmd_prefix(c, "SET_FEEDBACK", &args))            { handle_set_feedback(args);          return; }
        if (cmd_prefix(c, "SET_LINEFEED", &args))            { handle_set_linefeed(args);          return; }
        if (cmd_prefix(c, "SET_BRIGHT", &args))              { handle_set_bright(args);            return; }
        if (cmd_prefix(c, "SET_SCROLLMODE", &args))          { handle_set_scrollmode(args);        return; }
        if (cmd_prefix(c, "SET_SLEEPMODE", &args))           { handle_set_sleepmode(args);         return; }
        if (cmd_exact(c, "RESET_ALL"))                       { handle_reset_all();                 return; }
        if (cmd_prefix(c, "SET_NORMALVOLT", &args))          { handle_set_normalvolt(args);        return; }

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
        if (cmd_exact(c, "LIST_CONFIG")) { handle_query_list_config(); return; }
        if (cmd_exact(c, "HELP"))        { handle_query_help();        return; }
    }

    invalid_param();
}

// ── Arduino entry points ──────────────────────────────────────────────────────

void setup()
{
    Serial.begin(9600);

    // On ESP32 boards with USB-CDC, wait for the host to open the port.
    // Remove this loop if using a hardware UART (Serial1/Serial2).
    unsigned long t = millis();
    while (!Serial && (millis() - t) < 3000) {
        delay(10);
    }

#ifdef LED_BUILTIN
    // Blink the built-in LED once to indicate the emulator is ready.
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, HIGH);
    delay(200);
    digitalWrite(LED_BUILTIN, LOW);
#endif
}

void loop()
{
    while (Serial.available()) {
        char c = static_cast<char>(Serial.read());

        if (c == '\r') {
            // CR marks the end of a Furman protocol command.
            cmd_buf[cmd_len] = '\0';
            if (cmd_len > 0) {
                dispatch(cmd_buf);
            }
            cmd_len = 0;
        } else if (c == '\n') {
            // Ignore LF — some hosts send CRLF; the real device uses CR only.
        } else if (cmd_len < static_cast<int>(sizeof(cmd_buf) - 1)) {
            cmd_buf[cmd_len++] = c;
        }
        // If the buffer overflows, the next CR will dispatch a truncated
        // command, which will fall through to invalid_param().
    }
}
