#include "plate_spool.h"
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <sys/file.h>
#include <unistd.h>
namespace vb::plate {
namespace {
void write_all(int fd, const char* p, size_t n) { while(n){ ssize_t w=::write(fd,p,n); if(w<0){if(errno==EINTR)continue;throw std::runtime_error("spool write failed: "+std::string(std::strerror(errno)));} if(w==0)throw std::runtime_error("spool write made no progress"); p+=w;n-=static_cast<size_t>(w);} }
void fsync_file(int fd, const char* what) { if(::fsync(fd)!=0) throw std::runtime_error(std::string(what)+" fsync failed: "+std::strerror(errno)); }
uint64_t get_uint(const Json& j,const char* key){ if(!j.contains(key)||!j[key].is_number_unsigned())throw std::runtime_error(std::string("invalid spool ")+key); return j[key].get<uint64_t>(); }
}
Spool::Spool(std::string path,uint64_t max_events):path_(std::move(path)),cursor_path_(path_+".cursor"),lock_path_(path_+".lock"),max_events_(max_events){
 if(max_events_<1)throw std::invalid_argument("max_events must be >= 1");
 std::filesystem::path p(path_); std::filesystem::create_directories(p.parent_path());
 lock_fd_=::open(lock_path_.c_str(),O_RDWR|O_CREAT|O_CLOEXEC,0600); if(lock_fd_<0)throw std::runtime_error("spool lock open failed: "+std::string(std::strerror(errno)));
 if(::flock(lock_fd_,LOCK_EX|LOCK_NB)!=0){std::string e=std::strerror(errno);::close(lock_fd_);lock_fd_=-1;throw std::runtime_error("spool owner busy: "+e);} try{load();}catch(...){::flock(lock_fd_,LOCK_UN);::close(lock_fd_);lock_fd_=-1;throw;}
}
Spool::~Spool(){if(lock_fd_>=0){::flock(lock_fd_,LOCK_UN);::close(lock_fd_);}}
void Spool::ensure_healthy() const { if(poisoned_) throw std::runtime_error("spool poisoned after uncertain persistence failure"); }
void Spool::load(){
 if(std::filesystem::exists(cursor_path_)){std::ifstream f(cursor_path_);if(!f)throw std::runtime_error("spool cursor open failed");Json j;f>>j;acked_=get_uint(j,"acked");}
 if(std::filesystem::exists(path_)){
  std::ifstream f(path_,std::ios::binary);if(!f)throw std::runtime_error("spool journal open failed");
  std::string content((std::istreambuf_iterator<char>(f)),std::istreambuf_iterator<char>());
  if(f.bad())throw std::runtime_error("spool journal read failed");
  if(!content.empty()&&content.back()!='\n')throw std::runtime_error("spool incomplete tail");
  std::istringstream lines(content);std::string line;uint64_t previous=0;bool first=true;
  while(std::getline(lines,line)){
   if(line.empty()) throw std::runtime_error("spool blank journal line");
   Json j; try { j=Json::parse(line); }catch(...){throw std::runtime_error("spool corrupt journal line");}
   if(!j.is_object()||!j.contains("id")||!j.contains("topic")||!j.contains("payload")||!j["topic"].is_string()||!j["payload"].is_object())throw std::runtime_error("spool invalid journal record");
   uint64_t id=get_uint(j,"id");if(!first&&id<=previous)throw std::runtime_error("spool id order regression");first=false;previous=id;SpoolEntry e{id,j["topic"].get<std::string>(),j["payload"],true};entries_.push_back(std::move(e));
  }
 }
 for(const auto&e:entries_)if(e.id<=acked_)continue; else if(e.envelope.is_null())throw std::runtime_error("spool null envelope");
 if(!entries_.empty()){if(entries_.back().id==std::numeric_limits<uint64_t>::max())throw std::overflow_error("spool id exhausted");next_id_=entries_.back().id+1;}
 if(next_id_<=acked_){if(acked_==std::numeric_limits<uint64_t>::max())throw std::overflow_error("spool id exhausted");next_id_=acked_+1;}
 if(std::any_of(entries_.begin(),entries_.end(),[&](const SpoolEntry&e){return e.id<=acked_;})) compact();
}
void Spool::fsync_directory() const {auto dir=std::filesystem::path(path_).parent_path();int fd=::open(dir.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(fd<0)throw std::runtime_error("spool directory open failed: "+std::string(std::strerror(errno)));int r=::fsync(fd);int c=::close(fd);if(r!=0)throw std::runtime_error("spool directory fsync failed: "+std::string(std::strerror(errno)));if(c!=0)throw std::runtime_error("spool directory close failed: "+std::string(std::strerror(errno)));}
void Spool::append_record(const SpoolEntry&e){std::string line=Json{{"id",e.id},{"topic",e.topic},{"payload",e.envelope}}.dump();line.push_back('\n');int fd=::open(path_.c_str(),O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC,0600);if(fd<0)throw std::runtime_error("spool journal open append failed: "+std::string(std::strerror(errno)));try{write_all(fd,line.data(),line.size());fsync_file(fd,"spool journal");}catch(...){::close(fd);throw;}if(::close(fd)!=0)throw std::runtime_error("spool journal close failed: "+std::string(std::strerror(errno)));fsync_directory();}
uint64_t Spool::append(const std::string&topic,const Json&envelope){ensure_healthy();if(topic.empty()||!envelope.is_object()||!envelope.contains("event_id")||!envelope["event_id"].is_string()||envelope["event_id"].get<std::string>().empty()||!envelope.contains("seq")||!envelope["seq"].is_number_unsigned()||!envelope.contains("replayed")||!envelope["replayed"].is_boolean())throw std::invalid_argument("plate spool requires complete envelope");if(entries_.size()>=max_events_)overflow_=true;if(next_id_==std::numeric_limits<uint64_t>::max())throw std::overflow_error("spool id exhausted");SpoolEntry e{next_id_,topic,envelope,false};try{append_record(e);}catch(...){poisoned_=true;throw;}entries_.push_back(e);++next_id_;return e.id;}
void Spool::write_cursor(uint64_t acked){std::string tmp=cursor_path_+".tmp";std::string data=Json{{"acked",acked}}.dump();int fd=::open(tmp.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0600);if(fd<0)throw std::runtime_error("spool cursor open failed: "+std::string(std::strerror(errno)));try{write_all(fd,data.data(),data.size());fsync_file(fd,"spool cursor");}catch(...){::close(fd);throw;}if(::close(fd)!=0)throw std::runtime_error("spool cursor close failed: "+std::string(std::strerror(errno)));if(::rename(tmp.c_str(),cursor_path_.c_str())!=0)throw std::runtime_error("spool cursor rename failed: "+std::string(std::strerror(errno)));fsync_directory();}
void Spool::compact(){std::string tmp=path_+".tmp";int fd=::open(tmp.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0600);if(fd<0)throw std::runtime_error("spool compact open failed: "+std::string(std::strerror(errno)));try{for(const auto&e:entries_)if(e.id>acked_){std::string line=Json{{"id",e.id},{"topic",e.topic},{"payload",e.envelope}}.dump()+"\n";write_all(fd,line.data(),line.size());}fsync_file(fd,"spool compact");}catch(...){::close(fd);throw;}if(::close(fd)!=0)throw std::runtime_error("spool compact close failed: "+std::string(std::strerror(errno)));if(::rename(tmp.c_str(),path_.c_str())!=0)throw std::runtime_error("spool compact rename failed: "+std::string(std::strerror(errno)));fsync_directory();entries_.erase(std::remove_if(entries_.begin(),entries_.end(),[&](const SpoolEntry&e){return e.id<=acked_;}),entries_.end());}
std::vector<SpoolEntry> Spool::pending(bool replay)const{ensure_healthy();std::vector<SpoolEntry> out;for(const auto&e:entries_)if(e.id>acked_){SpoolEntry copy=e;copy.replayed=copy.replayed||replay;copy.envelope["replayed"]=copy.replayed;out.push_back(std::move(copy));}return out;}
void Spool::ack(uint64_t id){ensure_healthy();if(id<=acked_)return;if(entries_.empty()||id!=entries_.front().id||id<=acked_)throw std::invalid_argument("spool ack must target pending head");try{write_cursor(id);acked_=id;compact();}catch(...){poisoned_=true;throw;}}
}
