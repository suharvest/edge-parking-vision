#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include "vb/json.h"
namespace vb::plate {
struct SpoolEntry { uint64_t id = 0; std::string topic; Json envelope; bool replayed = false; };
class Spool {
 public:
  Spool(std::string path, uint64_t max_events);
  ~Spool();
  Spool(const Spool&) = delete;
  Spool& operator=(const Spool&) = delete;
  uint64_t append(const std::string& topic, const Json& envelope);
  std::vector<SpoolEntry> pending(bool replay = false) const;
  void ack(uint64_t id);
  uint64_t depth() const { return entries_.size(); }
  bool overflow() const { return overflow_; }
  uint64_t next_id() const { return next_id_; }
  const std::string& path() const { return path_; }
  bool poisoned() const { return poisoned_; }
 private:
  void ensure_healthy() const;
  void load();
  void append_record(const SpoolEntry& entry);
  void write_cursor(uint64_t acked);
  void compact();
  void fsync_directory() const;
  std::string path_, cursor_path_, lock_path_;
  uint64_t max_events_, acked_ = 0, next_id_ = 1;
  bool overflow_ = false;
  bool poisoned_ = false;
  int lock_fd_ = -1;
  std::vector<SpoolEntry> entries_;
};
}
