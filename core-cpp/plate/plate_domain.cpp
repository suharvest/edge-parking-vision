#include "plate_domain.h"
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <random>
#include <sstream>
#include <stdexcept>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace vb::plate {
namespace {
constexpr uint64_t kReserve = 20;
const std::string kProvinces = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼";
const std::string kLetters = "ABCDEFGHJKLMNPQRSTUVWXYZ";
const std::string kDigits = "0123456789";
const std::string kAlnum = kLetters + kDigits;

bool id_ok(const std::string& s) {
  if (s.empty() || s.size() > 32) return false;
  return std::all_of(s.begin(), s.end(), [](unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-';
  });
}
bool finite01(double x) { return std::isfinite(x) && x >= 0 && x <= 1; }
bool continuation(unsigned char c) { return (c & 0xc0) == 0x80; }
size_t utf8_width(const std::string& s, size_t i) {
  const unsigned char c = static_cast<unsigned char>(s[i]);
  if (c < 0x80) return 1;
  size_t n = 0;
  if (c >= 0xc2 && c <= 0xdf) n = 2;
  else if (c >= 0xe0 && c <= 0xef) n = 3;
  else if (c >= 0xf0 && c <= 0xf4) n = 4;
  else return 0;
  if (i + n > s.size()) return 0;
  for (size_t j = 1; j < n; ++j)
    if (!continuation(static_cast<unsigned char>(s[i + j]))) return 0;
  const unsigned char c1 = static_cast<unsigned char>(s[i + 1]);
  if ((c == 0xe0 && c1 < 0xa0) || (c == 0xed && c1 >= 0xa0) ||
      (c == 0xf0 && c1 < 0x90) || (c == 0xf4 && c1 >= 0x90)) return 0;
  return n;
}
std::vector<std::string> codepoints(const std::string& s) {
  std::vector<std::string> out;
  for (size_t i = 0; i < s.size();) {
    size_t n = utf8_width(s, i);
    if (n == 0) return {};
    out.emplace_back(s.substr(i, n)); i += n;
  }
  return out;
}
std::string uuid4() {
  std::array<unsigned char, 16> b{};
  std::random_device rd; for (auto& x : b) x = static_cast<unsigned char>(rd());
  b[6] = static_cast<unsigned char>((b[6] & 0x0f) | 0x40);
  b[8] = static_cast<unsigned char>((b[8] & 0x3f) | 0x80);
  std::ostringstream o; o << std::hex;
  for (size_t i=0;i<b.size();++i) { if (i==4||i==6||i==8||i==10) o << '-'; o << (b[i]>>4) << (b[i]&15); }
  return o.str();
}
void write_all(int fd, const char* data, size_t size) {
  while (size != 0) {
    ssize_t n = ::write(fd, data, size);
    if (n < 0) { if (errno == EINTR) continue; throw std::runtime_error("seq write failed: " + std::string(std::strerror(errno))); }
    if (n == 0) throw std::runtime_error("seq write made no progress");
    data += n; size -= static_cast<size_t>(n);
  }
}
}

std::string normalize_cn(const std::string& text) {
  std::string out;
  for (size_t i=0; i<text.size();) {
    unsigned char c = text[i];
    if (c == ' ' || c == '-' || c == '.' || (c == 0xc2 && i+1 < text.size() && static_cast<unsigned char>(text[i+1]) == 0xb7)) { i += c == 0xc2 ? 2 : 1; continue; }
    if (c < 0x80) { char u = static_cast<char>(c >= 'a' && c <= 'z' ? c - 'a' + 'A' : c); if (u == 'O') u='0'; if (u == 'I') u='1'; out.push_back(u); ++i; continue; }
    size_t n = utf8_width(text, i); if (n == 0) return {};
    out.append(text, i, n); i += n;
  }
  return out;
}

std::optional<std::string> plate_type_cn(const std::string& normalized) {
  auto cp = codepoints(normalized);
  if (cp.size() != 7 && cp.size() != 8) return std::nullopt;
  if (kProvinces.find(cp[0]) == std::string::npos || kLetters.find(cp[1]) == std::string::npos) return std::nullopt;
  for (size_t i=2; i<cp.size(); ++i) if (cp[i].size()!=1 || kAlnum.find(cp[i][0]) == std::string::npos) return std::nullopt;
  return cp.size() == 7 ? std::optional<std::string>("cn_blue") : std::optional<std::string>("cn_green");
}

Domain::Domain(Config config) : config_(std::move(config)) {
  if (!id_ok(config_.site_id) || !id_ok(config_.camera_id) || config_.seq_state_dir.empty() || !std::isfinite(config_.dedup_s) || config_.dedup_s < 0) throw std::invalid_argument("invalid plate domain config");
  std::filesystem::create_directories(config_.seq_state_dir);
  seq_path_ = config_.seq_state_dir + "/seq.json";
  reserve_block();
}
uint64_t Domain::read_reserved() const {
  std::ifstream f(seq_path_);
  if (!f) { if (std::filesystem::exists(seq_path_)) throw std::runtime_error("seq open failed"); return 0; }
  Json j; f >> j;
  if (!j.contains("reserved") || !j["reserved"].is_number_unsigned()) throw std::runtime_error("invalid seq.json");
  return j["reserved"].get<uint64_t>();
}
void Domain::reserve_block() {
  const std::string lock_path = seq_path_ + ".lock";
  const int lock_fd = ::open(lock_path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600);
  if (lock_fd < 0) throw std::runtime_error("seq lock open failed: " + std::string(std::strerror(errno)));
  if (::flock(lock_fd, LOCK_EX) != 0) { const std::string error = std::strerror(errno); ::close(lock_fd); throw std::runtime_error("seq lock acquire failed: " + error); }
  try {
    const uint64_t high = read_reserved();
    if (high > std::numeric_limits<uint64_t>::max() - kReserve) throw std::runtime_error("seq exhausted");
    const uint64_t candidate_high = high + kReserve;
    persist_reserved(candidate_high);
    next_ = high; reserved_ = candidate_high;
  } catch (...) { ::flock(lock_fd, LOCK_UN); ::close(lock_fd); throw; }
  ::flock(lock_fd, LOCK_UN); ::close(lock_fd);
}
void Domain::persist_reserved(uint64_t value) {
  const std::string tmp = seq_path_ + ".tmp." + std::to_string(static_cast<long long>(::getpid()));
  const std::string payload = Json{{"reserved", value}}.dump();
  const int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (fd < 0) throw std::runtime_error("seq temp open failed: " + std::string(std::strerror(errno)));
  try { write_all(fd, payload.data(), payload.size()); if (::fsync(fd) != 0) throw std::runtime_error("seq file fsync failed: " + std::string(std::strerror(errno))); }
  catch (...) { ::close(fd); throw; }
  if (::close(fd) != 0) throw std::runtime_error("seq temp close failed: " + std::string(std::strerror(errno)));
  if (::rename(tmp.c_str(), seq_path_.c_str()) != 0) throw std::runtime_error("seq rename failed: " + std::string(std::strerror(errno)));
  const int dir_fd = ::open(config_.seq_state_dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (dir_fd < 0) throw std::runtime_error("seq directory open failed: " + std::string(std::strerror(errno)));
  const int sync_rc = ::fsync(dir_fd); const int close_rc = ::close(dir_fd);
  if (sync_rc != 0) throw std::runtime_error("seq directory fsync failed: " + std::string(std::strerror(errno)));
  if (close_rc != 0) throw std::runtime_error("seq directory close failed: " + std::string(std::strerror(errno)));
}
std::optional<uint64_t> Domain::next_seq() {
  if (next_ >= reserved_) reserve_block();
  uint64_t seq=next_++;
  return seq;
}
uint64_t Domain::next_seq_for_test() { auto x=next_seq(); if(!x) throw std::runtime_error("seq persist failed"); return *x; }
std::optional<Json> Domain::on_text_read(const TextRead& r, double now, const SnapshotRef& snap) {
  if (!std::isfinite(now) || !finite01(r.min_char_conf) || r.votes < 1 || r.frames < 1 || !std::isfinite(static_cast<double>(r.wall_ms))) return std::nullopt;
  for (double x:r.bbox) if(!finite01(x)) return std::nullopt;
  std::string norm=normalize_cn(r.text); auto type=plate_type_cn(norm); if(!type) return std::nullopt;
  auto it=last_emit_.find(norm); if(it!=last_emit_.end() && now-it->second < config_.dedup_s) return std::nullopt;
  auto seq=next_seq(); if(!seq)return std::nullopt;
  Json body{{"plate",r.text},{"plate_normalized",norm},{"region","cn"},{"plate_type",*type},{"confidence",r.min_char_conf},{"votes",r.votes},{"frames",r.frames},{"bbox",Json::array({r.bbox[0],r.bbox[1],r.bbox[2],r.bbox[3]})},{"track_id",r.track_id}};
  if(!snap.present) body["snapshot"] = nullptr; else { if(snap.sha256.size()!=64||!std::all_of(snap.sha256.begin(),snap.sha256.end(),[](unsigned char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');})||snap.url.empty()||snap.width<1||snap.height<1)return std::nullopt; body["snapshot"]={{"url",snap.url},{"sha256",snap.sha256},{"width",snap.width},{"height",snap.height}}; }
  Json env={{"schema","parking.plate/1"},{"event_id",uuid4()},{"site_id",config_.site_id},{"camera_id",config_.camera_id},{"ts",r.wall_ms},{"seq",*seq},{"replayed",false}}; for(auto it=body.begin();it!=body.end();++it)env[it.key()]=it.value(); last_emit_[norm]=now; return env;
}
}
