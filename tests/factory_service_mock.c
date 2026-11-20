/* libfactory_service 的测试替身：行为由环境变量驱动，调用记录追加到 FACTORY_MOCK_TRACE。
 * 仅用于 x86-64 CI 环境验证 vendor-storage 身份来源，不接触真实 eMMC。
 * 环境变量：FACTORY_MOCK_SN_FILE / FACTORY_MOCK_KEY_FILE 指向内容文件；
 * FACTORY_MOCK_STATUS 非空时所有读取直接返回该错误码；FACTORY_MOCK_TRACE 为调用记录文件。 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void trace(const char *what) {
    const char *path = getenv("FACTORY_MOCK_TRACE");
    if (!path) return;
    FILE *f = fopen(path, "a");
    if (!f) return;
    fprintf(f, "%s\n", what);
    fclose(f);
}

/* constructor 在 dlopen 时立即留痕，用于区分"库已加载"与"读取接口被调用"。 */
__attribute__((constructor)) static void loaded(void) { trace("loaded"); }

static int env_status(void) {
    const char *status = getenv("FACTORY_MOCK_STATUS");
    return status && *status ? atoi(status) : 0;
}

static int read_env_file(const char *env, char *out, size_t cap) {
    const char *path = getenv(env);
    if (!path || !*path) return -7; /* FS_ERR_ITEM_NOT_FOUND */
    FILE *f = fopen(path, "rb");
    if (!f) return -7;
    size_t n = fread(out, 1, cap - 1, f);
    int bad = ferror(f);
    fclose(f);
    if (bad) return -5; /* FS_ERR_DEVICE_IO */
    out[n] = '\0';
    return 0;
}

int fs_read_sn(char *sn, size_t cap) {
    trace("sn");
    int status = env_status();
    if (status) return status;
    return read_env_file("FACTORY_MOCK_SN_FILE", sn, cap);
}

int fs_read_privkey(char *out, size_t cap) {
    trace("privkey");
    int status = env_status();
    if (status) return status;
    return read_env_file("FACTORY_MOCK_KEY_FILE", out, cap);
}

const char *fs_strerror(int status) {
    (void)status;
    return "mock factory service failure";
}
