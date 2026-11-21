// Command native 把 CatPaw IDE 薄桥导出为 c-shared 动态库，供 Python ctypes 调用。
// 只暴露固定宽度整数、字节指针和显式长度；不跨语言传 Go 指针或宿主对象。
package main

/*
#include <stdint.h>
#include <string.h>
*/
import "C"

import (
	"encoding/json"
	"io"
	"unsafe"

	"catboss/bridge"
)

func main() {}

//export CatpawBridgeVersion
func CatpawBridgeVersion() C.int32_t { return C.int32_t(bridge.Version) }

func copyErr(buf *C.char, n C.int, msg string) {
	if buf == nil || n <= 0 {
		return
	}
	b := []byte(msg)
	if len(b) > int(n)-1 {
		b = b[:int(n)-1]
	}
	dst := unsafe.Slice((*byte)(unsafe.Pointer(buf)), int(n))
	copy(dst, b)
	dst[len(b)] = 0
}

//export CatpawStart
func CatpawStart(auth, body, conversation, errBuf *C.char, errLen C.int) C.int64_t {
	req := bridge.Request{
		Auth: json.RawMessage(C.GoString(auth)),
		Body: json.RawMessage(C.GoString(body)),
	}
	if conversation != nil {
		req.Conversation = C.GoString(conversation)
	}
	h, err := bridge.Start(req)
	if err != nil {
		copyErr(errBuf, errLen, err.Error())
		return -1
	}
	return C.int64_t(h)
}

//export CatpawNext
func CatpawNext(h C.int64_t, buf *C.char, n C.int, errBuf *C.char, errLen C.int) C.int32_t {
	if buf == nil || n <= 0 {
		copyErr(errBuf, errLen, "输出缓冲无效")
		return -1
	}
	dst := unsafe.Slice((*byte)(unsafe.Pointer(buf)), int(n))
	read, err := bridge.Next(int64(h), dst)
	if err == io.EOF {
		return 0
	}
	if err != nil {
		copyErr(errBuf, errLen, err.Error())
		return -1
	}
	return C.int32_t(read)
}

//export CatpawCancel
func CatpawCancel(h C.int64_t) { bridge.Cancel(int64(h)) }

//export CatpawClose
func CatpawClose(h C.int64_t) { bridge.Close(int64(h)) }
