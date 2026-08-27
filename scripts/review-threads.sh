#!/usr/bin/env bash
# Shared, side-effect-free GraphQL reader for landing and queue checks.

_review_thread_comments_json() {
  local thread_id="$1" cursor="" response comments='[]' has_next
  local query
  # shellcheck disable=SC2016  # GraphQL variables must stay literal.
  query='query($threadId:ID!,$after:String){node(id:$threadId){... on PullRequestReviewThread{comments(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{author{login} body url}}}}}'
  while true; do
    if [ -z "$cursor" ]; then
      response="$(gh api graphql -f query="$query" -f threadId="$thread_id" 2>/dev/null)" || return 1
    else
      response="$(gh api graphql -f query="$query" -f threadId="$thread_id" -f after="$cursor" 2>/dev/null)" || return 1
    fi
    printf '%s' "$response" | jq -e '
      (.data.node.comments.nodes | type == "array")
      and (.data.node.comments.pageInfo.hasNextPage | type == "boolean")
    ' >/dev/null || return 1
    comments="$(printf '%s' "$response" | jq --argjson acc "$comments" \
      '$acc + .data.node.comments.nodes')" || return 1
    has_next="$(printf '%s' "$response" | jq -r '.data.node.comments.pageInfo.hasNextPage')"
    [ "$has_next" = "true" ] || break
    cursor="$(printf '%s' "$response" | jq -r '.data.node.comments.pageInfo.endCursor // empty')"
    [ -n "$cursor" ] || return 1
  done
  printf '%s\n' "$comments"
}

# review_threads_json <owner> <repo> <pr> -> all review threads with complete
# comments for every unresolved, current thread.
review_threads_json() {
  local owner="$1" repo="$2" pr="$3" cursor="" response threads='[]' normalized='[]'
  local has_next query thread thread_id comments
  # shellcheck disable=SC2016  # GraphQL variables must stay literal.
  query='query($owner:String!,$repo:String!,$number:Int!,$after:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated}}}}}'
  while true; do
    if [ -z "$cursor" ]; then
      response="$(gh api graphql -f query="$query" -f owner="$owner" -f repo="$repo" -F number="$pr" 2>/dev/null)" || return 1
    else
      response="$(gh api graphql -f query="$query" -f owner="$owner" -f repo="$repo" -F number="$pr" -f after="$cursor" 2>/dev/null)" || return 1
    fi
    printf '%s' "$response" | jq -e '
      (.data.repository.pullRequest.reviewThreads.nodes | type == "array")
      and (.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage | type == "boolean")
    ' >/dev/null || return 1
    threads="$(printf '%s' "$response" | jq --argjson acc "$threads" \
      '$acc + .data.repository.pullRequest.reviewThreads.nodes')" || return 1
    has_next="$(printf '%s' "$response" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage')"
    [ "$has_next" = "true" ] || break
    cursor="$(printf '%s' "$response" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty')"
    [ -n "$cursor" ] || return 1
  done

  while IFS= read -r thread; do
    comments='[]'
    if printf '%s' "$thread" | jq -e '.isResolved == false and .isOutdated == false' >/dev/null; then
      thread_id="$(printf '%s' "$thread" | jq -r '.id // empty')"
      [ -n "$thread_id" ] || return 1
      comments="$(_review_thread_comments_json "$thread_id")" || return 1
    fi
    normalized="$(jq -nc --argjson acc "$normalized" --argjson item "$thread" --argjson comments "$comments" \
      '$acc + [$item + {comments:{nodes:$comments}}]')" || return 1
  done < <(printf '%s' "$threads" | jq -c '.[]')

  jq -n --argjson nodes "$normalized" \
    '{data:{repository:{pullRequest:{reviewThreads:{nodes:$nodes}}}}}'
}
