// 通用文本桥接。复用已经实测的版本指纹、字符串分配器、任务调度和卸载保护。
// 单独生成 text_bridge.dll，不替换仍在运行的 selftest_bridge.dll。
#define WXBOT_EXTENDED_RECEIVE
#include "selftest_bridge.cpp"

struct TextConfig {
    TestConfig base;
    char target[128];
};
static_assert(sizeof(TextConfig) == 1688, "Unexpected text config ABI");

struct AtTextConfig { TextConfig base; char members[1024]; };
static_assert(sizeof(AtTextConfig) == 2712, "Unexpected at-text config ABI");

struct MentionContext { const char* members; bool applied; };
using MessageFactory = SharedMessage* (__fastcall*)(SharedMessage*);
static MessageFactory original_factory = nullptr;
static volatile LONG probe_builder_consumed = -1;
static volatile LONG probe_request_body_length = -1;
static volatile LONG probe_completion_destroys = 0;
// 普通发送同样会设置 completion_report_path，探针产物必须靠显式开关区分。
static volatile LONG probe_armed = 0;

static SharedMessage* __fastcall mention_factory(SharedMessage* output) {
    _InterlockedIncrement(&active_callbacks);
    SharedMessage* result = nullptr;
    __try {
        result = original_factory(output);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        result = nullptr;
    }
    // 布局或成员不符时降级为纯文本（applied 保持 false），绝不向宿主抛异常。
    __try {
        const auto context = factory_tls == TLS_OUT_OF_INDEXES ? nullptr :
            static_cast<MentionContext*>(TlsGetValue(factory_tls));
        if (context && output) {
            auto object = static_cast<unsigned char*>(output->object);
            if (object && *reinterpret_cast<uintptr_t*>(object) == core_base + 0x8e53588) {
                // 实机真正艾特样本：最后两个 std::string 分别为正文 +0x758、成员列表 +0x778。
                auto members = reinterpret_cast<SmallString*>(object + 0x778);
                if (members->length == 0 && members->capacity == 15 &&
                    set_native_string(members, context->members))
                    context->applied = true;
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
    }
    _InterlockedDecrement(&active_callbacks);
    return result;
}

static DWORD ensure_mention_factory() {
    if (factory_hook_created) return 0;
    MH_STATUS status = MH_Initialize();
    const bool initialized_here = status == MH_OK;
    if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) return 600 + status;
    factory_tls = TlsAlloc();
    if (factory_tls == TLS_OUT_OF_INDEXES) {
        if (initialized_here) MH_Uninitialize();
        return ERROR_NOT_ENOUGH_MEMORY;
    }
    void* target = reinterpret_cast<void*>(core_base + 0x6f4c70);
    status = MH_CreateHook(target, reinterpret_cast<void*>(&mention_factory), reinterpret_cast<void**>(&original_factory));
    if (status == MH_OK) {
        status = MH_EnableHook(target);
        if (status != MH_OK) MH_RemoveHook(target);
    }
    if (status != MH_OK) {
        TlsFree(factory_tls); factory_tls = TLS_OUT_OF_INDEXES;
        if (initialized_here) MH_Uninitialize();
        return 600 + status;
    }
    factory_hook_created = true;
    return 0;
}

static bool valid_target(const char* target) {
    size_t length = strnlen_s(target, 128);
    if (!length || length >= 128) return false;
    for (size_t index = 0; index < length; ++index) {
        unsigned char ch = target[index];
        if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
              (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '@' || ch == '-')) return false;
    }
    return true;
}

struct TextCompletion {
    volatile LONG references;
    uint32_t padding;
    void (__fastcall* invoke)(TextCompletion*, const unsigned char*);
    void (__fastcall* destroy)(TextCompletion*);
    TextConfig config;
    char members[1024];
};

static char completion_report[16384];
static volatile LONG completion_report_length = 0;
static wchar_t completion_report_path[MAX_PATH] = {0};

struct ReportBuffer { char* data; size_t capacity; size_t used; };

static void report_append(ReportBuffer* buffer, const char* text) {
    while (*text && buffer->used < buffer->capacity) buffer->data[buffer->used++] = *text++;
}

static void report_json_string(ReportBuffer* buffer, const char* text) {
    report_append(buffer, "\"");
    for (const unsigned char* current = reinterpret_cast<const unsigned char*>(text);
         *current && buffer->used < buffer->capacity; ++current) {
        if (*current == '"' || *current == '\\') {
            report_append(buffer, "\\");
            if (buffer->used < buffer->capacity) buffer->data[buffer->used++] = static_cast<char>(*current);
        } else if (*current < 32) {
            char escaped[8];
            std::snprintf(escaped, sizeof(escaped), "\\u%04x", *current);
            report_append(buffer, escaped);
        } else {
            buffer->data[buffer->used++] = static_cast<char>(*current);
        }
    }
    report_append(buffer, "\"");
}

static void flush_completion_report() {
    const LONG length = _InterlockedExchange(&completion_report_length, 0);
    if (length <= 0 || !completion_report_path[0]) return;
    FILE* log = nullptr;
    if (_wfopen_s(&log, completion_report_path, L"wb") == 0 && log) {
        std::fwrite(completion_report, 1, static_cast<size_t>(length), log);
        std::fclose(log);
    }
}

static void __fastcall text_completion_invoke(TextCompletion* self, const unsigned char* result) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        ReportBuffer buffer{completion_report, sizeof(completion_report) - 1, 0};
        char head[96];
        const bool success = result && result[0] == 1;
        std::snprintf(head, sizeof(head), "{\"stage\":21,\"exception\":0,\"native_status\":%u,\"target\":", success ? 0 : 1);
        report_append(&buffer, head);
        report_json_string(&buffer, self->config.target);
        report_append(&buffer, ",\"text\":");
        report_json_string(&buffer, self->config.base.text);
        report_append(&buffer, ",\"at_members\":");
        report_json_string(&buffer, self->members);
        char tail[320];
        std::snprintf(tail, sizeof(tail),
                      ",\"thread_id\":%lu,\"completion_success\":%s,\"probe\":"
                      "{\"builder_consumed\":%ld,\"request_body_length\":%ld,"
                      "\"completion_destroys\":%ld,\"lease_destroys\":%ld}}\n",
                      GetCurrentThreadId(), success ? "true" : "false",
                      probe_builder_consumed, probe_request_body_length,
                      probe_completion_destroys, probe_lease_destroys);
        report_append(&buffer, tail);
        completion_report[buffer.used] = 0;
        _InterlockedExchange(&completion_report_length, static_cast<LONG>(buffer.used));
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static void __fastcall text_completion_destroy(TextCompletion* self) {
    delete self;
    _InterlockedDecrement(&live_completions);
    _InterlockedIncrement(&probe_completion_destroys);
}

static DWORD send_text_on_task(const TextConfig* config, const char* members = nullptr) {
    if (!config || live_completions != 0) return ERROR_BUSY;
    TestResult report{};
    DWORD status = 0;
    wcsncpy_s(completion_report_path, config->base.log_path, _TRUNCATE);
    _InterlockedExchange(&completion_report_length, 0);
    __try {
        struct TextRequest { SmallString receiver; SmallString body; } request{};
        request.receiver.capacity = request.body.capacity = 15;
        SmallString description{};
        description.capacity = 15;
        if (!set_native_string(&request.receiver, config->target) ||
            !set_native_string(&request.body, config->base.text) ||
            !set_inline(&description, "wxbot.text")) {
            free_native_string(&request.receiver);
            free_native_string(&request.body);
            status = ERROR_INVALID_PARAMETER;
        } else {
            TextCompletion* completion = new TextCompletion{1, 0, text_completion_invoke, text_completion_destroy, *config, {}};
            if (members) strcpy_s(completion->members, members);
            _InterlockedIncrement(&live_completions);
            MentionContext mention{members, false};
            if (members && !TlsSetValue(factory_tls, &mention)) {
                text_completion_destroy(completion);
                free_native_string(&request.receiver);
                free_native_string(&request.body);
                status = ERROR_INVALID_DATA;
            } else {
                using Builder = void (__fastcall*)(void*, TextRequest*, SmallString*, TextCompletion**);
                reinterpret_cast<Builder>(core_base + 0x54b02f0)(nullptr, &request, &description, &completion);
                report.stage = 20;
                probe_builder_consumed = (completion == nullptr) ? 1 : 0;
                probe_request_body_length = static_cast<LONG>(request.body.length);
                // 构建器返回后校验艾特是否真正写入；未生效返回独立错误码，不当作成功。
                if (members && !mention.applied) status = 7;
                if (members) TlsSetValue(factory_tls, nullptr);
                if (completion) text_completion_destroy(completion);
                // 好友和群 ID 通常超过 SSO 的 15 字节，必须释放两份请求字符串。
                free_native_string(&request.receiver);
                free_native_string(&request.body);
            }
        }
    } __except (record_exception(GetExceptionInformation(), &report)) {
        write_report(config->base, report, "WXBOT_TEXT_SEND_EXCEPTION");
        status = 5;
    }
    return report.exception ? 5 : status;
}

static bool valid_mentions(const char* members) {
    size_t length = strnlen_s(members, 1024);
    if (!length || length >= 1024) return false;
    unsigned int count = 1;
    size_t part = 0;
    for (size_t index = 0; index < length; ++index) {
        unsigned char ch = members[index];
        if (ch == ',') {
            if (!part || part >= 128 || ++count > 8) return false;
            part = 0;
        } else {
            if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
                  (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '-')) return false;
            ++part;
        }
    }
    return part > 0 && part < 128;
}

struct AtTaskLease {
    AtTextConfig config;
    explicit AtTaskLease(const AtTextConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~AtTaskLease() { _InterlockedDecrement(&live_tasks); }
};

extern "C" __declspec(dllexport) DWORD WINAPI WxSendAtText(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const AtTextConfig config = *static_cast<const AtTextConfig*>(parameter);
    size_t target_length = strnlen_s(config.base.target, sizeof(config.base.target));
    if (config.base.base.size != sizeof(AtTextConfig) || config.base.base.pid != GetCurrentProcessId() ||
        !valid_target(config.base.target) || target_length < 9 ||
        std::strcmp(config.base.target + target_length - 9, "@chatroom") || !valid_mentions(config.members))
        return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.base.text, sizeof(config.base.base.text));
    if (!length || length >= sizeof(config.base.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    DWORD status = ensure_mention_factory();
    if (status != 0) return status;
    auto lease = std::make_shared<AtTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config.base, lease->config.members); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    status = schedule_task(config.base.base, &task);
    return status;
}

struct TextTaskLease {
    TextConfig config;
    explicit TextTaskLease(const TextConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~TextTaskLease() {
        _InterlockedDecrement(&live_tasks);
        _InterlockedIncrement(&probe_lease_destroys);
    }
};

extern "C" __declspec(dllexport) DWORD WINAPI WxSendText(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId() ||
        !valid_target(config.target)) return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.text, sizeof(config.base.text));
    if (!length || length >= sizeof(config.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    auto lease = std::make_shared<TextTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    return schedule_task(config.base, &task);
}

static bool try_read_source(const unsigned char* message, size_t offset, char* output, size_t capacity) {
    __try {
        // 真正艾特小号样本已核对：msgsource +0x38 是直接 std::string 指针。
        // 与 from/to/content 的 StringValue 包装不同。
        const auto string = *reinterpret_cast<const SmallString* const*>(message + offset);
        if (!string || !string->length || string->length >= capacity || string->capacity < string->length ||
            !(string->capacity == 15 || (string->capacity >= 16 && string->capacity <= 1024 * 1024))) return false;
        const char* bytes = string->capacity > 15 ? *reinterpret_cast<const char* const*>(string->data) : string->data;
        std::memcpy(output, bytes, static_cast<size_t>(string->length));
        output[string->length] = 0;
        return std::strstr(output, "<msgsource") || std::strstr(output, "<atuserlist");
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void sample_all_text(const uintptr_t* batch) {
    __try {
        if (!batch || batch[1] < batch[0] || (batch[1] - batch[0]) % 0x78 != 0 ||
            (batch[1] - batch[0]) / 0x78 > 512) return;
        for (uintptr_t address = batch[0]; address < batch[1]; address += 0x78) {
            const auto message = reinterpret_cast<const unsigned char*>(address);
            if (*reinterpret_cast<const uint32_t*>(message + 0x14) != 1) continue;
            char sender[128]{}, receiver[128]{}, text[1024]{};
            if (!read_pb_string(message, 8, sender, sizeof(sender)) || !valid_target(sender) ||
                !read_pb_string(message, 0x18, receiver, sizeof(receiver)) || !valid_target(receiver) ||
                !read_pb_string(message, 0x20, text, sizeof(text))) continue;
            // 接收瞬间复制元数据，不持有任何微信消息对象或字符串指针。
            char source[4096]{};
            if (!try_read_source(message, 0x38, source, sizeof(source))) source[0] = 0;
            enqueue_received(sender, receiver, text, source);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // 布局不匹配时放弃采样，真实客户端仍按原逻辑收消息。
    }
}

static uintptr_t __fastcall text_receive(void* manager, const uintptr_t* batch, uint8_t flag1, uint8_t flag2) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    __try {
        sample_all_text(batch);
        result = original_receive(manager, batch, flag1, flag2);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxStartTextReceiver(void* parameter) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    AcquireSRWLockExclusive(&receive_lock);
    receive_head = receive_count = receive_dropped = 0;
    receive_sequence = 0;
    ReleaseSRWLockExclusive(&receive_lock);
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) return 600 + status;
    void* target = reinterpret_cast<void*>(core_base + 0x17b9010);
    status = MH_CreateHook(target, reinterpret_cast<void*>(&text_receive), reinterpret_cast<void**>(&original_receive));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); MH_Uninitialize(); return 600 + status; }
    receive_hook_created = true;
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessage(void* parameter) {
    // 保留旧接口的 1312 字节边界，旧调用者不会被 V2 写越界。
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMessage message{};
    const DWORD code = WxPollFileHelperMessage(&message);
    std::memcpy(parameter, &message, 1312);
    *static_cast<uint32_t*>(parameter) = 1312;
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessageV2(void* parameter) {
    return WxPollFileHelperMessage(parameter);
}

// 完成报告在回调线程只写入内存；由清理线程调用本导出落盘，并写最终探针计数。
extern "C" __declspec(dllexport) DWORD WINAPI WxFlushReport(void*) {
    if (active_callbacks != 0 || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    flush_completion_report();
    if (probe_armed && completion_report_path[0]) {
        wchar_t probe_path[MAX_PATH];
        wcsncpy_s(probe_path, completion_report_path, _TRUNCATE);
        wcsncat_s(probe_path, L".probe.json", _TRUNCATE);
        FILE* log = nullptr;
        if (_wfopen_s(&log, probe_path, L"wb") == 0 && log) {
            std::fprintf(log,
                "{\"builder_consumed\":%ld,\"request_body_length\":%ld,"
                "\"completion_destroys\":%ld,\"lease_destroys\":%ld}\n",
                probe_builder_consumed, probe_request_body_length,
                probe_completion_destroys, probe_lease_destroys);
            std::fclose(log);
        }
        _InterlockedExchange(&probe_armed, 0);
    }
    return 0;
}

// 契约探针：做一次带插桩的发送，由完成报告回传四项契约的可观测信号。
extern "C" __declspec(dllexport) DWORD WINAPI WxProbeContracts(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    if (!valid_target(config.target)) return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.text, sizeof(config.base.text));
    if (!length || length >= sizeof(config.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    _InterlockedExchange(&probe_armed, 1);
    probe_builder_consumed = -1;
    probe_request_body_length = -1;
    probe_completion_destroys = 0;
    probe_lease_destroys = 0;
    auto lease = std::make_shared<TextTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    const DWORD status = schedule_task(config.base, &task);
    if (status != 0) _InterlockedExchange(&probe_armed, 0);
    return status;
}

// 定向只读探针：仅在手动发送 WXBOT_AT_PROBE 的消息中记录 msgsource 字符串。
// 不发送消息、不改写消息对象，用于核对该版本真实艾特的元数据布局。
static TextConfig mention_probe_config{};

static bool copy_small_string(const unsigned char* object, size_t offset, char* output, size_t size) {
    __try {
        const auto string = reinterpret_cast<const SmallString*>(object + offset);
        if (!string->length || string->length >= size || string->capacity < string->length ||
            !(string->capacity == 15 || (string->capacity >= 16 && string->capacity <= 1024 * 1024))) return false;
        const char* data = string->capacity == 15 ? string->data : *reinterpret_cast<const char* const*>(string->data);
        std::memcpy(output, data, static_cast<size_t>(string->length));
        output[string->length] = 0;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void capture_mention_source(SharedMessage* message) {
    __try {
        if (!message || !message->object) return;
        const auto object = static_cast<const unsigned char*>(message->object);
        if (*reinterpret_cast<const uintptr_t*>(object) != core_base + 0x8e53588) return;
        char body[1024]{};
        if (!copy_small_string(object, 0x758, body, sizeof(body)) || !std::strstr(body, "WXBOT_AT_PROBE")) return;
        if (_InterlockedCompareExchange(&triggered_once, 1, 0) != 0) return;
        FILE* log = nullptr;
        if (_wfopen_s(&log, mention_probe_config.base.log_path, L"wb") != 0 || !log) return;
        std::fputs("{\"vtable_rva\":\"0x8e53588\",\"fields\":[", log);
        bool comma = false;
        for (size_t offset = 0; offset + sizeof(SmallString) <= 0x798; offset += 8) {
            char value[4096]{};
            if (!copy_small_string(object, offset, value, sizeof(value))) continue;
            if (!std::strstr(value, "msgsource") && !std::strstr(value, "atuserlist") &&
                !std::strstr(value, "WXBOT_AT_PROBE") && !std::strstr(value, "wxid_") &&
                !std::strstr(value, "@chatroom")) continue;
            if (comma) std::fputc(',', log);
            std::fprintf(log, "{\"offset\":%llu,\"value\":", static_cast<unsigned long long>(offset));
            write_json_string(log, value);
            std::fputc('}', log);
            comma = true;
        }
        std::fputs("],\"vectors\":[", log);
        comma = false;
        for (size_t offset = 0; offset + 24 <= 0x798; offset += 8) {
            const auto begin = *reinterpret_cast<const uintptr_t*>(object + offset);
            const auto end = *reinterpret_cast<const uintptr_t*>(object + offset + 8);
            const auto capacity = *reinterpret_cast<const uintptr_t*>(object + offset + 16);
            if (begin < 0x10000 || end <= begin || end > capacity || end - begin > 256 ||
                (end - begin) % 32 || capacity - begin > 1024 * 1024) continue;
            char member[128]{};
            if (!copy_small_string(reinterpret_cast<const unsigned char*>(begin), 0, member, sizeof(member)) ||
                std::strncmp(member, "wxid_", 5)) continue;
            if (comma) std::fputc(',', log);
            std::fprintf(log, "{\"offset\":%llu,\"member\":", static_cast<unsigned long long>(offset));
            write_json_string(log, member);
            std::fputc('}', log);
            comma = true;
        }
        std::fputs("],\"object_hex\":\"", log);
        for (size_t offset = 0; offset < 0x790; ++offset) std::fprintf(log, "%02x", object[offset]);
        std::fputs("\"}\n", log);
        std::fclose(log);
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

static void* __fastcall mention_probe_send(void* manager, void* output, SharedMessage* message, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    void* result = nullptr;
    __try {
        capture_mention_source(message);
        result = original_send(manager, output, message, flag);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxArmMentionProbe(void* parameter) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    mention_probe_config = config;
    triggered_once = 0;
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) return 600 + status;
    void* target = reinterpret_cast<void*>(core_base + 0x178f100);
    status = MH_CreateHook(target, reinterpret_cast<void*>(&mention_probe_send), reinterpret_cast<void**>(&original_send));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); MH_Uninitialize(); return 600 + status; }
    hook_created = true;
    return 0;
}
