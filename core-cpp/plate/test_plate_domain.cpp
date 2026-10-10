#include "plate_domain.h"
#include <cassert>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
using namespace vb::plate;
namespace {
TextRead read(std::string text) { TextRead x; x.text=std::move(text); x.min_char_conf=.9; x.votes=3; x.frames=4; x.bbox={.1,.2,.8,.9}; x.wall_ms=1700000000000; x.track_id=7; return x; }
void expect_seq_block(const std::filesystem::path& dir, int child) { Domain d({"demo-lot", "gate-a", dir.string(), 30}); std::ofstream out(dir / ("child-" + std::to_string(child) + ".txt")); for (int i=0; i<20; ++i) out << d.next_seq_for_test() << '\n'; out.close(); }
}
int main() {
  auto root=std::filesystem::temp_directory_path()/ ("plate-domain-"+std::to_string(getpid())); std::filesystem::remove_all(root); std::filesystem::create_directories(root);
  { Domain d({"demo-lot","gate-a",root.string(),30}); auto e=d.on_text_read(read("粤 a12345"),0); assert(e&&(*e)["plate_normalized"]=="粤A12345"&&(*e)["plate_type"]=="cn_blue"&&(*e)["seq"]==0&&(*e)["snapshot"].is_null()); assert(!d.on_text_read(read("粤A12345"),10)); auto g=read("粤A12345"); g.track_id=8; SnapshotRef sr{true,"/snapshots/gate-a/1.jpg",std::string(64,'a'),1920,1080}; auto e2=d.on_text_read(g,31,sr); assert(e2&&(*e2)["seq"]==1&&(*e2)["snapshot"]["width"]==1920); auto e3=d.on_text_read(read("粤A123456"),32); assert(e3&&(*e3)["plate_type"]=="cn_green"); auto bad=read("粤A12345"); bad.bbox[1]=NAN; assert(!d.on_text_read(bad,100)); }
  { Domain d({"demo-lot","gate-a",root.string(),30}); assert(d.next_seq_for_test()==20); }
  assert(normalize_cn(std::string("粤A") + std::string({char(0xE2),char(0x82)})).empty()); assert(normalize_cn(std::string("粤A") + std::string({char(0xC0),char(0xAF)}) + "2345").empty()); assert(normalize_cn(std::string("粤A") + std::string({char(0xED),char(0xA0),char(0x80)})).empty()); assert(normalize_cn("粤A😀2345") == "粤A😀2345");
  auto live=root/"live"; std::filesystem::create_directories(live); { Domain a({"demo-lot","gate-a",live.string(),30}); Domain b({"demo-lot","gate-a",live.string(),30}); assert(a.next_seq_for_test()==0); assert(b.next_seq_for_test()==20); assert(a.next_seq_for_test()==1); assert(b.next_seq_for_test()==21); }
  auto concurrent=root/"concurrent"; std::filesystem::create_directories(concurrent); constexpr int kChildren=4; pid_t pids[kChildren]{}; for (int i=0;i<kChildren;++i) { pids[i]=fork(); assert(pids[i]>=0); if(pids[i]==0){ try { expect_seq_block(concurrent,i); _exit(0); } catch (...) { _exit(2); } } } for (pid_t pid:pids) { int status=0; assert(waitpid(pid,&status,0)==pid); assert(WIFEXITED(status)&&WEXITSTATUS(status)==0); }
  std::set<uint64_t> all; for(int i=0;i<kChildren;++i){std::ifstream f(concurrent/("child-"+std::to_string(i)+".txt")); uint64_t x; int n=0; while(f>>x){all.insert(x); ++n;} assert(n==20);} assert(all.size()==80 && *all.begin()==0 && *all.rbegin()==79); uint64_t expected=0; for(auto x:all) assert(x==expected++);
  { Domain d({"demo-lot","gate-a",concurrent.string(),30}); assert(d.next_seq_for_test()==80); }
  auto rollover=root/"rollover-failure"; std::filesystem::create_directories(rollover); { Domain d({"demo-lot","gate-a",rollover.string(),30}); for(int i=0;i<20;++i) assert(d.next_seq_for_test()==static_cast<uint64_t>(i)); assert(::chmod(rollover.c_str(),0500)==0); bool first=false,second=false; try { (void)d.next_seq_for_test(); } catch(const std::runtime_error&) { first=true; } try { (void)d.next_seq_for_test(); } catch(const std::runtime_error&) { second=true; } assert(first&&second); assert(::chmod(rollover.c_str(),0700)==0); }
  auto failure=root/"failure"; std::filesystem::create_directories(failure); std::filesystem::create_directory(failure/"seq.json.lock"); bool failed=false; try { Domain d({"demo-lot","gate-a",failure.string(),30}); } catch(const std::runtime_error&) { failed=true; } assert(failed);
  std::filesystem::remove_all(root); std::cout << "plate_domain PASS restart=20 concurrent_unique=80 batches=20 utf8=4 persistence_failure=1\n";
}
