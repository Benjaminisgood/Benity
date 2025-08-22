// ===== 图数据完整性检查和规范化 =====
(function () {
  // 获取全局 graphData 对象，如果不存在则使用空对象
  const gd = window.graphData || {};
  // 确保 nodes 和 edges 是数组，如果不是则使用空数组
  const nodes = Array.isArray(gd.nodes) ? gd.nodes : [];
  const edges = Array.isArray(gd.edges) ? gd.edges : [];

  // 创建一个用于显示调试信息的函数
  function note(msg) {
    console.log("[graph]", msg);
    // 查找或创建调试信息显示元素
    let el = document.getElementById("graph-debug-note");
    if (!el) {
      el = document.createElement("div");
      el.id = "graph-debug-note";
      // 设置调试信息元素的样式
      Object.assign(el.style, {
        position: "fixed", right: "12px", bottom: "12px", zIndex: 9999,
        background: "rgba(0,0,0,.6)", color: "#fff", padding: "6px 10px",
        fontFamily: "monospace", fontSize: "12px", borderRadius: "8px",
        pointerEvents: "none"
      });
      document.body.appendChild(el);
    }
    el.textContent = String(msg);
  }

  // 创建一个包含所有节点 ID 的 Set
  const idSet = new Set(nodes.map(n => n.id));
  // 辅助函数：获取对象的 ID 或返回原值
  const idOf = x => (x && typeof x === "object" && "id" in x) ? x.id : x;

  // 规范化边数据，确保 source 和 target 都是 ID
  const normalized = edges.map(e => ({ source: idOf(e.source), target: idOf(e.target) }));
  // 过滤掉无效的边（端点不在节点列表中）
  const filtered = normalized.filter(e => idSet.has(e.source) && idSet.has(e.target));
  const badCount = normalized.length - filtered.length;

  // 如果有无效边，输出警告
  if (badCount > 0) {
    console.warn("Dropped bad edges (endpoint not in nodes). Example:",
      normalized.find(e => !idSet.has(e.source) || !idSet.has(e.target)));
  }

  // 显示图数据统计信息
  note(`nodes=${nodes.length}, edges=${filtered.length}, badEdges(dropped)=${badCount}`);

  // 更新全局 graphData 对象，使用过滤后的边
  window.graphData = { nodes, edges: filtered };
})();

// 选择 SVG 元素
const svg = d3.select("#graph");

// 获取 SVG 元素的尺寸
function getSize() {
  const node = svg.node();
  const w = node.clientWidth || window.innerWidth || 800;
  const h = node.clientHeight || window.innerHeight || 600;
  return { w, h };
}
let { w: width, h: height } = getSize();

// 创建一个 SVG 组元素，用于缩放和平移
const g = svg.append("g");

// 初始化节点位置，给每个节点一个随机的初始位置
graphData.nodes.forEach(n => {
  n.x = (Math.random() - 0.5) * 200 + width / 2;
  n.y = (Math.random() - 0.5) * 200 + height / 2;
});

// 创建边元素,未来可以拓展更多属性
const linkElements = g.append("g").selectAll(".link")
  .data(graphData.edges)
  .enter().append("line")
  .attr("class", "link")
  .attr("stroke", d => {
    if (d.source.type === 'user' && d.target.type === 'event') return "#2ecc71";
    if (d.source.type === 'user' && d.target.type === 'affair') return "#f39c12";
    return "#8892a0";
  })
  .attr("stroke-width", d => d.weight ? Math.sqrt(d.weight) : 1)
  .attr("stroke-opacity", 0.8)
  .attr("stroke-dasharray", d => d.type === 'dashed' ? "5,5" : "none");

// 创建节点元素
const nodeElements = g.append("g").selectAll(".node")
  .data(graphData.nodes)
  .enter().append("path")
  .attr("class", d => "node " + (d.type === 'user' ? 'user-node' : (d.type === 'event' ? 'event-node' : 'affair-node')))
  .attr("d", d3.symbol()
      .type(d => d.type === 'user' ? d3.symbolCircle : (d.type === 'event' ? d3.symbolSquare : d3.symbolTriangle))
      .size(d => 100 + 50 * (d.degree || 0)))
  .attr("fill", d => d.type === 'user' ? (d.gender === '女' ? "#e74c3c" : "#3498db")
                                       : d.type === 'event' ? (d.current_count < d.capacity ? "#2ecc71" : "none")
                                                            : (d.valid ? "#f39c12" : "none"))
  .attr("stroke", d => d.type === 'user'
      ? d3.color(d.gender === '女' ? "#e74c3c" : "#3498db").darker()
      : "#000")
  .attr("stroke-width", 1)
  .style("pointer-events", "all")
  .call(d3.drag().on("start", dragStart).on("drag", dragging).on("end", dragEnd))
  .on("click", nodeClicked);

// 为每个节点添加标题（鼠标悬停时显示）
nodeElements.append("title")
  .text(d => d.type === 'user' ? d.nickname : d.type === 'event' ? d.name : (d.content || '').slice(0, 20));

// 创建力导向图模拟
const simulation = d3.forceSimulation(graphData.nodes)
  .force("link", d3.forceLink(graphData.edges).id(d => d.id).distance(90))
  .force("charge", d3.forceManyBody().strength(-250))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collision", d3.forceCollide().radius(d => Math.sqrt((100 + 50 * (d.degree || 0)) / Math.PI) + 6))
  .alpha(1)
  .restart();

// 定义模拟的每一帧要执行的函数
simulation.on("tick", () => {
  // 更新边的位置
  linkElements
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  // 更新节点的位置
  g.selectAll("path.node").attr("transform", d => `translate(${d.x},${d.y})`);
});

// 添加缩放和平移功能
svg.call(d3.zoom().scaleExtent([0.1, 4]).on("zoom", (event) => {
  g.attr("transform", event.transform);
}));

// 当窗口大小改变时，重新计算图的中心
window.addEventListener("resize", () => {
  const sz = getSize();
  width = sz.w; height = sz.h;
  simulation.force("center", d3.forceCenter(width / 2, height / 2));
  simulation.alpha(0.5).restart();
});

// 启用缩放和平移
svg.call(d3.zoom().scaleExtent([0.1, 4]).on("zoom", (event) => {
    g.attr("transform", event.transform);
}));

// 如果有指定需要聚焦的节点（例如通过搜索进入）
if (window.focusTarget && window.focusTarget.length > 0) {
    const targetNode = graphData.nodes.find(n => n.id === window.focusTarget);
    if (targetNode) {
        // 将目标节点暂时固定在视图中心
        simulation.alpha(1).restart();
        targetNode.fx = width/2;
        targetNode.fy = height/2;
        simulation.tick(50);
        targetNode.fx = null;
        targetNode.fy = null;
        simulation.alpha(0);
        // 显示该节点的信息气泡
        showInfoBubble(targetNode);
    }
}

// 拖拽事件处理函数
function dragStart(event, d) {
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x;
    d.fy = d.y;
}

function dragging(event, d) {
    d.fx = event.x;
    d.fy = event.y;
}

function dragEnd(event, d) {
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null;
    d.fy = null;
}

// 节点点击事件处理函数
function nodeClicked(event, d) {
    showInfoBubble(d);
    event.stopPropagation();
}

// 显示节点信息气泡
function showInfoBubble(d) {
    const bubble = document.getElementById("infoBubble");
    let html = "";
    // 根据节点类型（用户、事件、事务）生成不同的 HTML 内容
    if (d.type === "user") {
        html += "<strong>" + d.nickname + "</strong>";
        html += "<p>" + (d.intro || "") + "</p>";
        html += `<p>👍 ${d.likes || 0}  👎 ${d.dislikes || 0}</p>`;
        if (d.id !== "u" + currentUserId) {
        html += ` <a href="/like_user/${d.raw_id}">点赞</a> <a href="/dislike_user/${d.raw_id}">差评</a>`;
        }
        if (d.id !== "u" + currentUserId) {
            const targetIdNum = parseInt(d.id.substring(1));
            const isFollowing = followingList.includes(targetIdNum);
            if (isFollowing) {
                html += `<a href="/unfollow/${targetIdNum}">取消关注</a>`;
            } else {
                html += `<a href="/follow/${targetIdNum}">关注</a>`;
            }
        } else {
            html += "<p>(这是您自己)</p>";
        }
    } else if (d.type === "event") {
        html += "<strong>" + d.name + "</strong>";
        html += "<p>" + (d.intro || "") + "</p>";
        html += "<p>时间: " + (d.time || "") + "</p>";
        html += "<p>地点: " + (d.location || "") + "</p>";
        html += "<p>组织者: " + (d.anon_organizer ? "匿名" : d.organizer_nickname) + "</p>";
        html += `<p>人数: ${d.current_count || 0} / ${d.capacity}</p>`;
        // 如果当前用户是组织者，可以编辑活动
        if ((d.anon_organizer && d.organizer_id === currentUserId) || (!d.anon_organizer && d.organizer_id === currentUserId)) {
            html += `<a href="/event/edit/${d.raw_id}">编辑</a>`;
        }
        const eid = d.raw_id;
        html += `<p>👍 ${d.likes || 0}  👎 ${d.dislikes || 0}</p>`;
        html += `<a href="/like_event/${eid}">点赞</a> <a href="/dislike_event/${eid}">差评</a>`;
        let isJoined = false;
        let joinedAnon = false;
        if (joinedEvents.includes(eid)) {
            isJoined = true;
        } else if (anonJoinedEvents.includes(eid)) {
            isJoined = true;
            joinedAnon = true;
        }
        if (!isJoined && (!d.capacity || d.current_count < d.capacity)) {
            html += ` <a href="/join_event/${eid}">参加</a> <a href="/join_event_anon/${eid}">匿名参加</a>`;
        }
        if (isJoined) {
            if (joinedAnon) {
                html += ` <a href="/leave_event/${eid}">取消匿名参与</a>`;
            } else {
                html += ` <a href="/leave_event/${eid}">取消参与</a>`;
            }
        }
    } else if (d.type === "affair") {
        html += "<p>" + d.content + "</p>";
        if (d.link) {
            html += `<p><a href="${d.link}" target="_blank">查看链接</a></p>`;
        }
        html += `<p>👍 ${d.likes || 0}  👎 ${d.dislikes || 0}</p>`;
        html += `<a href="/like_affair/${d.raw_id}">点赞</a> <a href="/dislike_affair/${d.raw_id}">差评</a>`;
        // 留言列表
        html += "<div><strong>留言:</strong>";
        if (d.comments && d.comments.length > 0) {
            html += "<ul>";
            d.comments.forEach(cmt => {
                html += "<li>" + cmt + "</li>";
            });
            html += "</ul>";
        }
        // 留言提交表单
        html += `<form action="/add_comment/${d.raw_id}" method="post" style="margin-top:5px;">
                    <input type="text" name="comment" placeholder="输入留言...">
                    <input type="submit" value="留言">
                 </form>`;
        html += "</div>";
        // 如果当前用户是发布者，可以编辑与切换有效性
        if (d.poster_id === currentUserId) {
            html += ` <a href="/affair/edit/${d.raw_id}">编辑</a>`;
            if (d.valid) {
                html += ` <a href="/toggle_affair/${d.raw_id}">标记为失效</a>`;
            } else {
                html += ` <a href="/toggle_affair/${d.raw_id}">标记为有效</a>`;
            }
        }
    }
    bubble.innerHTML = html;
    // 定位气泡到节点附近
    const point = d3.zoomTransform(svg.node()).apply([d.x, d.y]);
    bubble.style.left = point[0] + 15 + "px";
    bubble.style.top = point[1] + 15 + "px";
    bubble.style.display = "block";
}

// 点击空白处隐藏信息气泡
svg.on("click", () => {
    document.getElementById("infoBubble").style.display = "none";
});

// 夜间模式切换函数
function toggleDarkMode() {
    const body = document.body;
    const btn = document.querySelector(".menu button");
    if (body.classList.contains("dark-mode")) {
        body.classList.remove("dark-mode");
        btn.innerText = "夜间模式";
        localStorage.setItem("darkMode", "off");
    } else {
        body.classList.add("dark-mode");
        btn.innerText = "日间模式";
        localStorage.setItem("darkMode", "on");
    }
}

// 页面加载时根据本地存储设置模式
if (localStorage.getItem("darkMode") === "on") {
    document.body.classList.add("dark-mode");
    const btn = document.querySelector(".menu button");
    if (btn) btn.innerText = "日间模式";
}