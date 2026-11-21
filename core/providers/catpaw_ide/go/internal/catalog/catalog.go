// Package catalog 是 Catboss catalog.Model 的最小本地定义，仅承载 IDE 目录字段。
package catalog

type Model struct {
	ID       string
	Name     string
	Provider string
	APIs     []string
	RPM      int
	Images   bool
	Context  int
	Efforts  []string
	Serves   string
}

// ServesOf 在薄桥中没有目录索引，返回空串表示未知。
func ServesOf(string) string { return "" }
