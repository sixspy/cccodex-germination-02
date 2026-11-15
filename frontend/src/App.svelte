<script>
  let profile = null;
  let username = "admin";
  let password = "123456";
  let loginError = "";
  let trials = [];
  let detail = null;
  let formError = "";
  let busy = false;
  let pinnedChoice = "";

  try {
    profile = JSON.parse(localStorage.getItem("germ.profile") || "null");
  } catch {
    profile = null;
  }

  async function request(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = localStorage.getItem("germ.token") || "";
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "请求没有完成");
    return data;
  }

  async function refresh(prefer) {
    trials = await request("/api/trials");
    const target = prefer || detail?.trial.id || trials[0]?.id;
    if (target) await loadTrial(target);
  }

  async function loadTrial(id) {
    detail = await request(`/api/trials/${id}`);
    pinnedChoice = detail.drop_setting?.algorithm || "";
  }

  async function submitLogin() {
    loginError = "";
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("germ.token", data.access_token);
      profile = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("germ.profile", JSON.stringify(profile));
      await refresh();
    } catch (error) {
      loginError = error.message;
    }
  }

  async function post(path, body) {
    busy = true;
    formError = "";
    try {
      detail = await request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      pinnedChoice = detail.drop_setting?.algorithm || "";
      trials = await request("/api/trials");
    } catch (error) {
      formError = error.message;
      try {
        trials = await request("/api/trials");
        const id = detail?.trial.id;
        if (id) await loadTrial(id);
      } catch {
        /* 保留原错误 */
      }
    } finally {
      busy = false;
    }
  }

  function pinSetting() {
    if (!detail || !pinnedChoice) return;
    return post(`/api/trials/${detail.trial.id}/drop-setting`, { algorithm: pinnedChoice });
  }

  function revise(algorithm) {
    if (!detail) return;
    return post(`/api/trials/${detail.trial.id}/revise`, { algorithm });
  }

  function publishCurrent() {
    if (!detail || detail.over_limit.length) return;
    return post(`/api/trials/${detail.trial.id}/publish`);
  }

  function queueHint(row) {
    if (row.queue_state === "未钉口径") return "剔床盘未钉死，禁止修订";
    if (row.queue_state === "已修订") return "本日已修订入账";
    return "已钉口径，等待修订";
  }

  function logout() {
    localStorage.removeItem("germ.token");
    localStorage.removeItem("germ.profile");
    profile = null;
    detail = null;
  }

  function previewFor(kind) {
    if (kind === "void_batch") {
      return { rate: "—", detail: "全批不入账，超限粒不进分母" };
    }
    const beds = (detail?.beds || []).filter((bed) => bed.state === "normal");
    const sown = beds.reduce((sum, bed) => sum + bed.sown, 0);
    const sprouted = beds.reduce((sum, bed) => sum + bed.sprouted, 0);
    const moldy = beds.reduce((sum, bed) => sum + bed.moldy, 0);
    const tested = sown - moldy;
    if (tested <= 0 || sprouted > tested) {
      return { rate: "—", detail: "剔除超限床后没有可入账的床" };
    }
    const bp = Math.floor((sprouted * 10000) / tested);
    return {
      rate: `${Math.floor(bp / 100)}.${String(bp % 100).padStart(2, "0")}%`,
      detail: `分母 ${tested} · 剔除霉粒 ${moldy} · 发芽 ${sprouted} · 超限粒不进分母`,
    };
  }

  if (profile) {
    refresh().catch(() => logout());
  }
</script>

{#if !profile}
  <main class="paper gate">
    <p class="kicker">种子检验室</p>
    <h1>发芽口径核算台</h1>
    <p>现行口径：分母是播种粒数减去霉粒数，分子是发芽粒数。霉变种子不计入可计种子。</p>
    <form on:submit|preventDefault={submitLogin}>
      <label>用户<input bind:value={username} /></label>
      <label>口令<input type="password" bind:value={password} /></label>
      <button>打开记录</button>
      {#if loginError}<p class="warn">{loginError}</p>{/if}
    </form>
  </main>
{:else}
  <header class="bar">
    <div>
      <p class="kicker">口径核算</p>
      <h1>发芽口径核算台</h1>
    </div>
    <div class="who">
      <span>{profile.title} {profile.username}</span>
      <button class="quiet" on:click={logout}>离开</button>
    </div>
  </header>
  <main class="desk">
    <aside>
      <section class="queue">
        <h3>过限队列</h3>
        {#if trials.some((row) => row.over_limit_count > 0)}
          {#each trials.filter((row) => row.over_limit_count > 0) as row}
            <button class="queue-row" class:on={detail && detail.trial.id === row.id} on:click={() => loadTrial(row.id)}>
              <strong>{row.lot_code}</strong>
              <span>超限床 {row.over_limit_count} · 口径 {row.setting_name}</span>
              <em>{queueHint(row)}</em>
            </button>
          {/each}
        {:else}
          <p class="rule">队列里没有超限批。</p>
        {/if}
      </section>
      {#each trials as trial}
        <button class:on={detail && detail.trial.id === trial.id} on:click={() => loadTrial(trial.id)}>
          <strong>{trial.lot_code}</strong>
          <em>{trial.crop_name}</em>
          <span>{trial.status}</span>
          <span class="tags">
            {#if trial.over_limit_count > 0}<i class="tag hot">超限床 {trial.over_limit_count}</i>{/if}
            <i class="tag">口径 {trial.setting_name}</i>
            {#if trial.queue_state}<i class="tag">{trial.queue_state}</i>{/if}
            {#if trial.voided > 0}<i class="tag">挡下 {trial.voided}</i>{/if}
            {#if trial.dropped > 0}<i class="tag">丢掉 {trial.dropped}</i>{/if}
          </span>
        </button>
      {/each}
    </aside>
    {#if detail}
      <article class="paper">
        <div class="head">
          <h2>{detail.trial.lot_code} · {detail.trial.crop_name}</h2>
          <button disabled={profile.role !== "writer" || busy || detail.over_limit.length > 0} on:click={publishCurrent}>
            按现行口径出数
          </button>
        </div>
        <p>{detail.trial.status}</p>
        {#if detail.over_limit.length && profile.role === "writer"}
          <p class="warn">超限批不能直接出数：先钉剔床口径，再按与口径相符的算法修订入账。</p>
        {/if}
        <p class="rule">
          现行口径的分母是播种粒数减去霉粒数。旧口径把霉粒留在分母里，出过的数字不会被抹掉。
          播种粒空白的床不进过限队列。
        </p>
        {#if profile.role !== "writer"}<p>见证员只能核对，不能钉剔床口径，也不能修订超限批。</p>{/if}
        {#if formError}<p class="warn">{formError}</p>{/if}

        <section class="block">
          <h3>剔床设置</h3>
          {#if detail.drop_setting}
            <p class="pinned">
              本日已钉死：<strong>{detail.drop_setting.algorithm_name}</strong>
              · {detail.drop_setting.pinned_by} · {detail.drop_setting.pinned_at}
              · 口径到 {detail.drop_setting.day} 日为止，明日才能改
            </p>
          {:else}
            <p class="rule">剔床盘未钉死之前禁止修订超限批。口径一旦钉下，本日不能再改。</p>
            <div class="choices">
              {#each detail.algorithms as option}
                <label class="choice">
                  <input type="radio" bind:group={pinnedChoice} value={option.value} disabled={profile.role !== "writer" || busy} />
                  <span><strong>{option.name}</strong><em>{option.label}</em></span>
                </label>
              {/each}
            </div>
            <button disabled={profile.role !== "writer" || busy || !pinnedChoice} on:click={pinSetting}>钉死剔床口径</button>
          {/if}
        </section>

        <section class="block">
          <h3>超限列表</h3>
          <p class="counts">
            挡下回数 <strong>{detail.counts.voided}</strong>
            · 丢掉回数 <strong>{detail.counts.dropped}</strong>
            · 口径名 <strong>{detail.counts.setting_name}</strong>
          </p>
          {#if detail.over_limit.length}
            <table>
              <thead>
                <tr><th>床</th><th>播种</th><th>发芽</th><th>霉粒</th><th>超限粒</th><th>处理</th></tr>
              </thead>
              <tbody>
                {#each detail.over_limit as bed}
                  <tr class="over">
                    <td>{bed.bed_no}</td>
                    <td>{bed.sown}</td>
                    <td>{bed.sprouted}</td>
                    <td>{bed.moldy}</td>
                    <td>{bed.excess}</td>
                    <td>挡下，不入账</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:else}
            <p class="rule">本批没有超限床。</p>
          {/if}
          {#if detail.blank_beds.length}
            <p class="rule">播种粒空白、不进过限队列的床：{detail.blank_beds.join("、")}</p>
          {/if}
        </section>

        {#if detail.over_limit.length}
          <section class="block">
            <h3>修订条目</h3>
            <p class="rule">两种算法分开列出，只有跟剔床设置相符的一项可以入账。</p>
            {#if !detail.drop_setting}
              <p class="rule">剔床盘未钉死之前禁止修订超限批。</p>
            {:else if detail.revised_today}
              <p class="rule">这一超限批本日已经修订入账，慢一步的修订会被记作剔床争用。</p>
            {/if}
            <div class="cards">
              {#each detail.algorithms as option}
                {@const preview = previewFor(option.value)}
                {@const aligned = detail.drop_setting?.algorithm === option.value}
                <section class="card" class:aligned>
                  <strong>{option.label}</strong>
                  <span class="word">{option.name}</span>
                  <span class="rate">{preview.rate}</span>
                  <em>{preview.detail}</em>
                  <button
                    disabled={profile.role !== "writer" || busy || !detail.revise_ready || !aligned}
                    on:click={() => revise(option.value)}
                  >
                    {aligned ? "按此修订" : "与剔床设置不符"}
                  </button>
                </section>
              {/each}
            </div>
          </section>
        {/if}

        <section class="block">
          <h3>原床详情</h3>
          <table>
            <thead>
              <tr><th>床</th><th>播种</th><th>发芽</th><th>霉粒</th><th>原床发芽率</th><th>修订后发芽率</th><th>去向</th></tr>
            </thead>
            <tbody>
              {#each detail.beds as bed}
                <tr class:over={bed.state === "over_limit"} class:blank={bed.state === "blank"}>
                  <td>{bed.bed_no}</td>
                  <td>{bed.sown === null ? "空白" : bed.sown}</td>
                  <td>{bed.sprouted}</td>
                  <td>{bed.moldy}</td>
                  <td>{bed.original_rate_label || "—"}</td>
                  <td>{bed.revised_rate_label || "—"}</td>
                  <td>{bed.revised_note}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </section>

        <h3>出数记录</h3>
        {#each detail.publications as row}
          <section class="edition" class:old={!row.current} class:voided={row.voided}>
            <strong>
              {#if row.revision_kind}{row.kind_label}{:else}{row.current ? "现行口径" : "旧口径"}{/if}
              {row.rate_label}
            </strong>
            {#if row.revision_kind}
              <span>
                {row.voided ? "全批不作数" : `分母 ${row.tested} · 剔除霉粒 ${row.excluded_mold} · 发芽 ${row.sprouted}`}
                · 剔床算法 {row.algorithm_name}
                {#if row.excluded_beds}· 剔除床次 {row.excluded_beds}{/if}
                {#if row.original_rate_label}· 原床发芽率 {row.original_rate_label}{/if}
              </span>
            {:else}
              <span>分母 {row.tested} · 剔除霉粒 {row.excluded_mold} · 发芽 {row.sprouted}</span>
            {/if}
            <em>{row.note} · {row.published_by} · {row.published_at}</em>
          </section>
        {/each}

        {#if detail.disputes.length}
          <section class="block">
            <h3>争用与跑偏记录</h3>
            {#each detail.disputes as row}
              <p class="dispute">
                <strong>{row.outcome_label}</strong>
                <span>{row.username} · 报文 {row.requested_name || "未填"}{#if row.setting_name} · 设置 {row.setting_name}{/if}</span>
                <em>{row.detail} · {row.created_at}</em>
              </p>
            {/each}
          </section>
        {/if}
      </article>
    {/if}
  </main>
{/if}

<style>
  .bar, .desk, .gate { width: min(1100px, calc(100% - 32px)); margin: 0 auto; }
  .bar { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 8px; }
  .kicker { margin: 0 0 4px; letter-spacing: 0.16em; color: #b4332a; font-size: 13px; }
  h1, h2, h3 { font-weight: 560; margin: 0; }
  .who { display: flex; gap: 12px; align-items: center; }
  .desk { display: grid; grid-template-columns: 260px 1fr; gap: 18px; padding-bottom: 40px; }
  .paper { background: rgba(255, 250, 242, 0.94); padding: 22px; box-shadow: 0 10px 30px rgba(44, 36, 22, 0.05); }
  .gate { margin-top: 40px; width: min(520px, calc(100% - 32px)); }
  form, aside { display: grid; gap: 10px; }
  label { display: grid; gap: 6px; }
  input { border: 0; border-bottom: 1px solid #b4332a; background: transparent; padding: 8px 0; color: #2c2416; }
  button { background: #b4332a; color: #fffaf2; border: 0; padding: 8px 12px; }
  .quiet { background: transparent; color: #2c2416; border-bottom: 1px solid #2c2416; }
  aside button { text-align: left; background: transparent; color: #2c2416; border-bottom: 1px solid rgba(180, 51, 42, 0.35); display: grid; gap: 2px; }
  aside button.on { background: #fffaf2; }
  aside em, .edition em, .card em, .dispute em { font-style: normal; color: #7a6a55; }
  .queue { display: grid; gap: 6px; margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px dashed rgba(180, 51, 42, 0.5); }
  .queue h3 { font-size: 15px; color: #b4332a; letter-spacing: 0.08em; }
  .queue .rule { margin: 0; font-size: 13px; }
  .queue-row { border-bottom: 1px dotted rgba(180, 51, 42, 0.35); }
  .queue-row em { font-size: 12px; }
  .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .tag { font-style: normal; font-size: 12px; color: #7a6a55; border: 1px solid rgba(180, 51, 42, 0.3); padding: 1px 6px; }
  .tag.hot { color: #b4332a; border-color: #b4332a; }
  .head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
  .rule { color: #7a6a55; }
  .block { margin: 18px 0; padding-top: 12px; border-top: 1px dashed rgba(180, 51, 42, 0.4); display: grid; gap: 8px; }
  .counts { margin: 0; }
  .pinned { margin: 0; color: #2c2416; }
  .choices { display: flex; flex-wrap: wrap; gap: 12px; }
  .choice { display: flex; gap: 8px; align-items: center; border: 1px solid rgba(180, 51, 42, 0.35); padding: 8px 10px; }
  .choice span { display: grid; }
  .choice em { font-style: normal; color: #7a6a55; font-size: 12px; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .card { display: grid; gap: 4px; padding: 12px; border: 1px solid rgba(180, 51, 42, 0.35); }
  .card.aligned { border-color: #b4332a; background: rgba(180, 51, 42, 0.06); }
  .card .word { font-size: 12px; color: #7a6a55; }
  .card .rate { font-size: 22px; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0 16px; }
  th, td { text-align: left; padding: 8px 4px; border-bottom: 1px solid rgba(180, 51, 42, 0.25); }
  tr.over td { color: #b4332a; }
  tr.blank td { color: #7a6a55; }
  .edition { display: grid; gap: 4px; padding: 10px 0; border-top: 1px dashed rgba(180, 51, 42, 0.4); }
  .old strong { color: #7a6a55; }
  .voided strong { color: #8a3b2a; }
  .dispute { display: grid; gap: 2px; margin: 0; padding: 8px 0; border-bottom: 1px solid rgba(180, 51, 42, 0.2); }
  .warn { color: #b4332a; }
  @media (max-width: 800px) {
    .desk { grid-template-columns: 1fr; }
    .cards { grid-template-columns: 1fr; }
  }
</style>
