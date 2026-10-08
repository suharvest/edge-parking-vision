#pragma once
#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <memory>
#include "vb/json.h"

namespace vb::plate {

struct TextRead {
  std::string text;
  double min_char_conf = 0;
  int votes = 0;
  int frames = 0;
  std::array<double, 4> bbox{};
  int64_t wall_ms = 0;
  uint32_t track_id = 0;
};

struct SnapshotRef {
  bool present = false;
  std::string url;
  std::string sha256;
  int width = 0;
  int height = 0;
};

struct Config {
  std::string site_id;
  std::string camera_id;
  std::string seq_state_dir;
  double dedup_s = 30.0;
};

std::string normalize_cn(const std::string& text);
std::optional<std::string> plate_type_cn(const std::string& normalized);

class Domain {
 public:
  explicit Domain(Config config);
  // now_s is monotonic seconds, supplied by the service for deterministic tests.
  std::optional<Json> on_text_read(const TextRead& read, double now_s,
                                    const SnapshotRef& snapshot = {});
  uint64_t next_seq_for_test();
  const std::string& seq_path() const { return seq_path_; }

 private:
  uint64_t read_reserved() const;
  void reserve_block();
  void persist_reserved(uint64_t value);
  std::optional<uint64_t> next_seq();
  Config config_;
  std::string seq_path_;
  uint64_t next_ = 0, reserved_ = 0;
  std::unordered_map<std::string, double> last_emit_;
};

}  // namespace vb::plate
