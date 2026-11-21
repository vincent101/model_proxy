// Package lazyre 是 Catboss lazyre 的最小本地替代：编译期不需要，标准库 regexp 语义一致。
package lazyre

import "regexp"

type Regexp struct{ re *regexp.Regexp }

func New(pattern string) *Regexp { return &Regexp{re: regexp.MustCompile(pattern)} }

func (r *Regexp) MatchString(s string) bool { return r.re.MatchString(s) }

func (r *Regexp) FindStringSubmatch(s string) []string { return r.re.FindStringSubmatch(s) }

func (r *Regexp) ReplaceAllString(src, repl string) string { return r.re.ReplaceAllString(src, repl) }
