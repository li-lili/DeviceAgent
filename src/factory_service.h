/*
 * factory_service.h - read SN and device keys from Rockchip Vendor Storage.
 *
 * Runs on the RK3588 Ubuntu board and talks to the raw eMMC device
 * (default /dev/mmcblk0).  The on-disk layout is Rockchip Vendor Storage.
 *
 * Read-only API:
 *     int fs_read_sn(char *sn, size_t cap);
 *     int fs_read_pubkey(char *out, size_t cap);
 *     int fs_read_privkey(char *out, size_t cap);
 *     const char *fs_strerror(int status);
 *
 * Vendor Storage items:
 *     ID 1    SN                      (Rockchip Vendor Storage)
 *     ID 100  RSA public key          (OpenSSH single line)
 *     ID 101  RSA private key         (PEM text)
 *
 * The read-only API is at the top.  SN write and provisioning helpers are in
 * the "Advanced / provisioning API" section of this same header; the native
 * serial agent and provisioning tools use them.
 */
#ifndef FACTORY_SERVICE_H
#define FACTORY_SERVICE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32)
#  if defined(FACTORY_SERVICE_BUILD)
#    define FS_API __declspec(dllexport)
#  else
#    define FS_API __declspec(dllimport)
#  endif
#elif defined(__GNUC__) && __GNUC__ >= 4
#  define FS_API __attribute__((visibility("default")))
#else
#  define FS_API
#endif

/* Vendor Storage item IDs.  RESERVED/used IDs 1..31 are Rockchip's; 100/101
 * are in the vendor-defined range (32..65535). */
#define FS_VENDOR_SN_ID       1u
#define FS_VENDOR_PUBKEY_ID   100u
#define FS_VENDOR_PRIVKEY_ID  101u

/* SN: max 508 bytes + NUL. */
#define FS_SN_MAX_LEN 508u
#define FS_SN_BUFSZ   (FS_SN_MAX_LEN + 1u)

/* Keys: RSA-2048 PEM/OpenSSH text fits well within this; +1 for NUL. */
#define FS_KEY_MAX_LEN 8192u
#define FS_KEY_BUFSZ   (FS_KEY_MAX_LEN + 1u)

/* Default raw device; override with the environment variable below. */
#define FS_DEFAULT_DEVICE "/dev/mmcblk0"
#define FS_DEVICE_ENV "FACTORY_SERVICE_SN_DEV"

/*
 * Return codes.  FS_OK (0) means success; failures are negative values.
 * Values are part of the ABI.
 */
typedef enum fs_status {
    FS_OK                     = 0,
    FS_ERR_INVALID_ARGUMENT   = -1,  /* NULL argument, bad length           */
    FS_ERR_BUFFER_TOO_SMALL   = -2,  /* output buffer too small             */
    FS_ERR_PERMISSION         = -3,  /* EACCES/EPERM; run as root/disk group */
    FS_ERR_DEVICE_OPEN        = -4,  /* device missing/unopenable           */
    FS_ERR_DEVICE_IO          = -5,  /* short read, EIO, fsync failure      */
    FS_ERR_NO_VALID_SLOT      = -6,  /* no valid Vendor Storage slot        */
    FS_ERR_ITEM_NOT_FOUND     = -7,  /* requested item ID not in newest slot */
    FS_ERR_CORRUPT            = -8,  /* malformed header/item table/bounds  */
    FS_ERR_SN_INVALID         = -9,  /* SN empty or non-printable ASCII     */
    FS_ERR_SN_TOO_LONG        = -10, /* SN longer than FS_SN_MAX_LEN        */
    FS_ERR_NO_SPACE           = -11, /* item table or data area full        */
    FS_ERR_LOCK               = -12, /* could not lock the device in 5 s    */
    FS_ERR_VERIFY             = -13, /* read-back after write did not match */
    FS_ERR_INTERNAL           = -14, /* allocation or version exhaustion    */
    FS_ERR_KEY_INVALID        = -15, /* key content not a valid OpenSSH key */
    FS_ERR_KEY_TOO_LONG       = -16  /* key longer than FS_KEY_MAX_LEN      */
} fs_status;

/*
 * Read the SN from the newest valid Vendor Storage slot.
 *
 * sn   Output buffer, NUL-terminated on success; printable ASCII.
 * cap  Size of sn; use FS_SN_BUFSZ (509).
 *
 * Returns FS_OK or a negative status (FS_ERR_ITEM_NOT_FOUND,
 * FS_ERR_NO_VALID_SLOT, FS_ERR_DEVICE_*, FS_ERR_PERMISSION, ...).
 */
FS_API int fs_read_sn(char *sn, size_t cap);

/*
 * Read the OpenSSH RSA public key (Vendor Storage item 100).
 *
 * out  Output buffer, NUL-terminated; trailing CR/LF is stripped.
 * cap  Size of out; use FS_KEY_BUFSZ (8193).
 *
 * Returns FS_OK, FS_ERR_ITEM_NOT_FOUND if not provisioned,
 * FS_ERR_KEY_INVALID if the stored item is not an ssh-rsa/ssh-ed25519 line,
 * FS_ERR_KEY_TOO_LONG, FS_ERR_BUFFER_TOO_SMALL, or a device error.
 */
FS_API int fs_read_pubkey(char *out, size_t cap);

/*
 * Read the RSA private key (Vendor Storage item 101).
 *
 * The key is returned exactly as provisioned (PEM text, including newlines).
 * Treat it as a secret: do not log it or send it over untrusted channels.
 *
 * out  Output buffer, NUL-terminated.
 * cap  Size of out; use FS_KEY_BUFSZ (8193).
 */
FS_API int fs_read_privkey(char *out, size_t cap);

/* Returns a static human-readable string for a status code. */
FS_API const char *fs_strerror(int status);

/* -------------------------------------------------------------------------
 * Advanced / provisioning API
 *
 * Normal readers only need the API above.  These functions are used by the
 * native factory_service agent, the CLI and provisioning tools.
 * ------------------------------------------------------------------------- */

#define FS_ABI_VERSION 1u

/* Library ABI/version information. */
FS_API uint32_t fs_abi_version(void);
FS_API const char *fs_version_string(void);

/*
 * Device used when `device` is NULL/"": $FACTORY_SERVICE_SN_DEV, then
 * FS_DEFAULT_DEVICE.  Returned pointer is owned by the environment/library.
 */
FS_API const char *fs_default_device(void);

/*
 * Generic item read from the newest valid slot.
 *
 * out      Buffer, or NULL with cap == 0 for a size query.
 * out_len  Required/actual item length.  Set even when
 *          FS_ERR_BUFFER_TOO_SMALL is returned.
 *
 * Returns FS_OK, FS_ERR_ITEM_NOT_FOUND, FS_ERR_BUFFER_TOO_SMALL,
 * FS_ERR_CORRUPT, or a device error.  Content is not validated here.
 */
FS_API int fs_read_item_at(const char *device, uint16_t id,
                           void *out, size_t cap, size_t *out_len);

/*
 * Provisioning-only generic item write.
 *
 * Writes the item to the slot after the newest valid slot, preserving all
 * other Vendor Storage items, fsync's, and verifies the read-back.  Used by
 * `factory_service keys-store` to write IDs 100/101.
 *
 * `len` must be 1..64504.  Each call creates a new slot.  This function
 * refuses item 1 (SN); use fs_write_sn_at() for the SN.
 */
FS_API int fs_write_item_at(const char *device, uint16_t id,
                            const void *data, size_t len);

/*
 * Read the SN (item 1) from an explicit device, with the same validation as
 * fs_read_sn().  Used by the native factory_service agent, which may be
 * configured with --device instead of $FACTORY_SERVICE_SN_DEV.
 *
 * Returns FS_OK and a NUL-terminated printable-ASCII SN, or a negative status.
 */
FS_API int fs_read_sn_at(const char *device, char *sn, size_t cap);

/*
 * Read the OpenSSH public key (item 100) from an explicit device.
 * Same content rules as fs_read_pubkey().
 */
FS_API int fs_read_pubkey_at(const char *device, char *out, size_t cap);

/*
 * Factory-production SN write (item 1).
 *
 * `sn` must be 1..FS_SN_MAX_LEN printable ASCII bytes; no trailing newline.
 * The write builds a complete new Vendor Storage slot, preserves all other
 * items, fsync's, and verifies the read-back, exactly like fs_write_item_at().
 */
FS_API int fs_write_sn_at(const char *device, const void *sn, size_t len);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* FACTORY_SERVICE_H */
