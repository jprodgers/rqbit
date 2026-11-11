// Changed in the rqbit fork's copy of this crate: upstream compiles a C file
// (`lladdr.c`) whose one function is the `LLADDR` macro of <net/if_dl.h>,
//
//     #define LLADDR(s) ((caddr_t)((s)->sdl_data + (s)->sdl_nlen))
//
// applied to an interface's address. It is written in Rust here so that no C
// compiler is needed to build on macOS and the BSDs.

use libc::ifaddrs;

/// The link-level (MAC) address in an `AF_LINK` interface address: it follows
/// the interface name in the `sockaddr_dl`.
///
/// # Safety
///
/// `ptr` must point to an `ifaddrs` from `getifaddrs` whose `ifa_addr` is a
/// `sockaddr_dl` (address family `AF_LINK`).
#[cfg(any(
    target_os = "macos",
    target_os = "ios",
    target_os = "tvos",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd",
    target_os = "dragonfly",
    target_os = "illumos",
))]
pub unsafe fn lladdr(ptr: *mut ifaddrs) -> *const u8 {
    let sdl = (*ptr).ifa_addr as *const libc::sockaddr_dl;
    let data = std::ptr::addr_of!((*sdl).sdl_data) as *const u8;
    data.add(usize::from((*sdl).sdl_nlen))
}
